# assistant_tools.py — Tool definitions and execution for the dashboard AI assistant
#
# Pattern follows support_tools.py but with key differences:
# - No consent gate (user is authenticated, acting on their own account)
# - Direct access to GHL APIs via user's tokens
# - Can execute write actions (book, send SMS) without asking permission
#
# Tools: search_contact, send_sms, check_calendar, book_appointment,
#         get_call_stats, navigate_dashboard, get_contact_intelligence

import json
import logging
import re
import requests

logger = logging.getLogger(__name__)

# ── Secret scrubber (reused from support_tools.py pattern) ────────────────────

_SECRET_PATTERNS = [
    (re.compile(r'AC[a-f0-9]{32}'), '[REDACTED]'),
    (re.compile(r'SK[a-f0-9]{32}'), '[REDACTED]'),
    (re.compile(r'BU[a-f0-9]{32}'), '[REDACTED]'),
    (re.compile(r'PN[a-f0-9]{32}'), '[REDACTED]'),
    (re.compile(r'auth_token[\s:="\']+[a-f0-9]{32}', re.IGNORECASE), 'auth_token=[REDACTED]'),
]


def _scrub(data):
    """Remove secrets from tool results."""
    text = json.dumps(data) if isinstance(data, (dict, list)) else str(data)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


# ── Tool definitions ──────────────────────────────────────────────────────────

def get_assistant_tool_definitions():
    """Return OpenAI-compatible tool schemas for the dashboard assistant."""
    return [
        {
            "type": "function",
            "function": {
                "name": "search_contact",
                "description": "Search for a contact in the user's CRM by name or phone number. Returns contact details including name, phone, email, tags, and pipeline stage. Always call this before sending SMS or booking — you need the contact_id.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Contact name (e.g. 'John Smith') or phone number (e.g. '+15551234567')"
                        },
                        "search_type": {
                            "type": "string",
                            "enum": ["name", "phone"],
                            "description": "Search by name or phone number. Default: name"
                        }
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "send_sms",
                "description": "Send an SMS text message to a contact. Requires the contact_id (use search_contact first if you only have a name).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_id": {
                            "type": "string",
                            "description": "The GHL contact ID"
                        },
                        "message": {
                            "type": "string",
                            "description": "The text message to send"
                        }
                    },
                    "required": ["contact_id", "message"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "check_calendar",
                "description": "Check available appointment time slots on the user's calendar for the next few days.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days_ahead": {
                            "type": "integer",
                            "description": "Number of days to look ahead (default 3, max 14)"
                        }
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "book_appointment",
                "description": "Book an appointment for a contact at a specific date and time. Requires contact_id (use search_contact first) and a time expression.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_id": {
                            "type": "string",
                            "description": "The GHL contact ID"
                        },
                        "selected_time": {
                            "type": "string",
                            "description": "The appointment time, e.g. 'Tuesday at 2:30 PM', 'tomorrow at 4pm', '2026-03-25 14:00'"
                        },
                        "first_name": {
                            "type": "string",
                            "description": "Contact's first name (for calendar event title)"
                        }
                    },
                    "required": ["contact_id", "selected_time"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_call_stats",
                "description": "Get the user's call statistics including total calls, connected calls, connect rate, and talk time.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "period": {
                            "type": "string",
                            "enum": ["today", "week", "month", "all"],
                            "description": "Time period for stats. Default: today"
                        }
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "navigate_dashboard",
                "description": "Navigate the user to a specific dashboard tab. Use when they ask to go somewhere or see a specific page.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "destination": {
                            "type": "string",
                            "description": "Where to navigate, e.g. 'voice settings', 'billing', 'dialer', 'team', 'workflows'"
                        }
                    },
                    "required": ["destination"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_contact_intelligence",
                "description": "Get AI-powered intelligence analysis for a contact including temperature (hot/warm/cool/cold), engagement score, summary, and recommended next actions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_id": {
                            "type": "string",
                            "description": "The GHL contact ID"
                        }
                    },
                    "required": ["contact_id"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "make_call",
                "description": "Initiate an outbound phone call to a contact. Requires contact_id and phone number (use search_contact first). If the user hasn't specified whether AI or they should talk, call WITHOUT dial_mode to ask them first.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_id": {
                            "type": "string",
                            "description": "The GHL contact ID"
                        },
                        "phone": {
                            "type": "string",
                            "description": "The contact's phone number"
                        },
                        "first_name": {
                            "type": "string",
                            "description": "Contact's first name"
                        },
                        "dial_mode": {
                            "type": "string",
                            "enum": ["ai", "human"],
                            "description": "Who handles the call: 'ai' for AI voice agent, 'human' for the user to talk directly. ONLY set this if the user explicitly chose. Omit to ask them."
                        }
                    },
                    "required": ["contact_id", "phone"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "queue_dial_session",
                "description": "Queue contacts from a pipeline stage for a power dial session. Fetches all contacts in the specified pipeline/stage and starts dialing. Use when the user says things like 'call everyone in New Leads' or 'dial my hot leads' or 'power dial the Qualified stage'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pipeline_name": {
                            "type": "string",
                            "description": "Name of the pipeline (e.g. 'Sales Pipeline', 'New Leads')"
                        },
                        "stage_name": {
                            "type": "string",
                            "description": "Name of the stage within the pipeline (e.g. 'Qualified', 'Hot Leads', 'Follow Up'). If not specified, queues ALL contacts in the pipeline."
                        },
                        "dial_mode": {
                            "type": "string",
                            "enum": ["ai", "human"],
                            "description": "Who handles connected calls: 'ai' for AI voice agent, 'human' for the user. Default: ai"
                        }
                    },
                    "required": ["pipeline_name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "query_call_history",
                "description": "Search call history with filters. Use for: 'show calls over 5 minutes', 'did anyone call me today?', 'missed calls', 'who did I talk to longest?', 'show my recordings', 'inbound calls today'. Returns call records with contact name, duration, direction, and status.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "period": {"type": "string", "enum": ["today", "yesterday", "week", "month", "all"], "description": "Time period. Default: today"},
                        "direction": {"type": "string", "enum": ["inbound", "outbound", "all"], "description": "Call direction filter. Default: all"},
                        "min_duration_seconds": {"type": "integer", "description": "Minimum call duration in seconds (e.g. 300 for 5 minutes)"},
                        "status": {"type": "string", "enum": ["completed", "no-answer", "busy", "failed", "all"], "description": "Call status filter. Use 'no-answer' for missed calls. Default: all"},
                        "has_recording": {"type": "boolean", "description": "Only show calls with recordings"},
                        "sort": {"type": "string", "enum": ["duration_desc", "duration_asc", "recent", "oldest"], "description": "Sort order. Default: recent"},
                        "limit": {"type": "integer", "description": "Max results (default 10, max 50)"}
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_hot_leads",
                "description": "Get the user's hottest leads ranked by AI intelligence score. Use for: 'who should I call first?', 'show my hottest leads', 'who's ready to buy?', 'priority leads', 'best leads to call'. Returns contacts sorted by temperature and score.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "temperature": {"type": "string", "enum": ["hot", "warm", "cool", "cold", "all"], "description": "Filter by AI temperature. Default: hot"},
                        "limit": {"type": "integer", "description": "Max results (default 10, max 30)"},
                        "should_respond_only": {"type": "boolean", "description": "Only show leads where AI says you should respond now. Default: false"}
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_upcoming_appointments",
                "description": "Check upcoming appointments on the user's calendar. Use for: 'what's on my calendar today?', 'do I have appointments tomorrow?', 'when's my next call?', 'show my schedule'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days_ahead": {"type": "integer", "description": "Days to look ahead (default 1, max 7)"}
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_recent_messages",
                "description": "Get recent SMS messages. Use for: 'who texted me?', 'any new messages?', 'show texts from today', 'who responded?', 'unread messages', 'did anyone reply?'. Returns recent inbound messages with contact info.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "direction": {"type": "string", "enum": ["inbound", "outbound", "all"], "description": "Message direction. Use 'inbound' for 'who texted me'. Default: inbound"},
                        "period": {"type": "string", "enum": ["today", "yesterday", "week"], "description": "Time period. Default: today"},
                        "limit": {"type": "integer", "description": "Max results (default 10, max 30)"}
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_stale_leads",
                "description": "Find leads that haven't been contacted recently. Use for: 'who hasn't been contacted in a week?', 'stale leads', 'leads going cold', 'who needs follow up?', 'neglected contacts', 'who should I follow up with?'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days_since_contact": {"type": "integer", "description": "Minimum days since last contact (default 7)"},
                        "limit": {"type": "integer", "description": "Max results (default 15, max 50)"}
                    },
                    "required": []
                }
            }
        },
        # ── Additional Queries ──────────────────────────────────────────
        {"type": "function", "function": {
            "name": "get_pipeline_summary",
            "description": "Get a summary of pipeline stages with contact counts. Use for: 'how many leads in each stage?', 'pipeline overview', 'funnel summary'.",
            "parameters": {"type": "object", "properties": {
                "pipeline_name": {"type": "string", "description": "Pipeline name (optional — shows all if omitted)"},
            }, "required": []}
        }},
        {"type": "function", "function": {
            "name": "search_recordings",
            "description": "Search call recordings, optionally filtered by contact or date. Use for: 'find my recording with John', 'show recordings from Monday', 'play back my last call'.",
            "parameters": {"type": "object", "properties": {
                "contact_name": {"type": "string", "description": "Filter by contact name"},
                "period": {"type": "string", "enum": ["today", "yesterday", "week", "month"], "description": "Default: week"},
                "limit": {"type": "integer", "description": "Default 10"},
            }, "required": []}
        }},
        {"type": "function", "function": {
            "name": "send_bulk_sms",
            "description": "Send the same SMS message to multiple contacts by pipeline stage or tag. Use for: 'text everyone in New Leads saying we have a special offer', 'blast my hot leads with a follow up'.",
            "parameters": {"type": "object", "properties": {
                "message": {"type": "string", "description": "Message to send to all contacts"},
                "pipeline_name": {"type": "string"}, "stage_name": {"type": "string"},
                "tag": {"type": "string", "description": "Alternative: filter by tag"},
                "limit": {"type": "integer", "description": "Max contacts to text (default 25, max 50)"},
            }, "required": ["message"]}
        }},
        {"type": "function", "function": {
            "name": "get_daily_summary",
            "description": "Get a comprehensive daily summary: calls made, messages sent, appointments booked, hot leads, missed calls. Use for: 'give me my daily summary', 'how did today go?', 'end of day report', 'daily recap'.",
            "parameters": {"type": "object", "properties": {
                "period": {"type": "string", "enum": ["today", "yesterday"], "description": "Default: today"},
            }, "required": []}
        }},

        # ── Contact Management ──────────────────────────────────────────
        {"type": "function", "function": {
            "name": "edit_contact",
            "description": "Update a contact's fields: name, email, phone, address, tags, custom fields. Use for: 'update John's email', 'change phone number', 'add tag VIP'.",
            "parameters": {"type": "object", "properties": {
                "contact_id": {"type": "string", "description": "Contact ID (search first)"},
                "first_name": {"type": "string"}, "last_name": {"type": "string"},
                "email": {"type": "string"}, "phone": {"type": "string"},
                "address1": {"type": "string"}, "city": {"type": "string"},
                "state": {"type": "string"}, "postal_code": {"type": "string"},
            }, "required": ["contact_id"]}
        }},
        {"type": "function", "function": {
            "name": "add_contact_tag",
            "description": "Add one or more tags to a contact. Use for: 'tag John as VIP', 'add hot-lead tag'.",
            "parameters": {"type": "object", "properties": {
                "contact_id": {"type": "string", "description": "Contact ID"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags to add"},
            }, "required": ["contact_id", "tags"]}
        }},
        {"type": "function", "function": {
            "name": "remove_contact_tag",
            "description": "Remove a tag from a contact.",
            "parameters": {"type": "object", "properties": {
                "contact_id": {"type": "string"},
                "tag": {"type": "string", "description": "Tag to remove"},
            }, "required": ["contact_id", "tag"]}
        }},
        {"type": "function", "function": {
            "name": "add_contact_note",
            "description": "Add a note to a contact's record. Use for: 'add a note on John saying he prefers term life', 'note that Jane called back'.",
            "parameters": {"type": "object", "properties": {
                "contact_id": {"type": "string"},
                "note": {"type": "string", "description": "The note text"},
            }, "required": ["contact_id", "note"]}
        }},
        {"type": "function", "function": {
            "name": "create_contact",
            "description": "Create a new contact in the CRM. Use for: 'add a new lead John Smith 555-1234'.",
            "parameters": {"type": "object", "properties": {
                "first_name": {"type": "string"}, "last_name": {"type": "string"},
                "phone": {"type": "string"}, "email": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            }, "required": ["first_name", "phone"]}
        }},
        {"type": "function", "function": {
            "name": "move_contact_pipeline",
            "description": "Move a contact to a different pipeline stage. Use for: 'move John to Qualified', 'move to booked stage'.",
            "parameters": {"type": "object", "properties": {
                "contact_id": {"type": "string"},
                "pipeline_name": {"type": "string", "description": "Pipeline name"},
                "stage_name": {"type": "string", "description": "Stage name to move to"},
            }, "required": ["contact_id", "stage_name"]}
        }},
        {"type": "function", "function": {
            "name": "get_contact_history",
            "description": "Get full conversation + call history for a contact. Use for: 'show me my conversation with John', 'what did Jane say?', 'call history with this contact'.",
            "parameters": {"type": "object", "properties": {
                "contact_id": {"type": "string"},
                "limit": {"type": "integer", "description": "Max messages (default 20)"},
            }, "required": ["contact_id"]}
        }},
        {"type": "function", "function": {
            "name": "mark_do_not_contact",
            "description": "Mark a contact as Do Not Contact (DNC). Use for: 'DNC this contact', 'mark as do not call', 'remove from calling list'.",
            "parameters": {"type": "object", "properties": {
                "contact_id": {"type": "string"},
            }, "required": ["contact_id"]}
        }},

        # ── Workflows ──────────────────────────────────────────────────
        {"type": "function", "function": {
            "name": "list_workflows",
            "description": "List all workflows with their status (active/paused/draft). Use for: 'show my workflows', 'what automations do I have?'.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }},
        {"type": "function", "function": {
            "name": "assign_contact_to_workflow",
            "description": "Enroll a contact into a workflow. Use for: 'put John in the Speed to Lead workflow', 'assign to re-engagement'.",
            "parameters": {"type": "object", "properties": {
                "contact_id": {"type": "string"},
                "workflow_name": {"type": "string", "description": "Name of the workflow"},
            }, "required": ["contact_id", "workflow_name"]}
        }},
        {"type": "function", "function": {
            "name": "assign_bulk_to_workflow",
            "description": "Enroll multiple contacts into a workflow by pipeline stage or tag. Use for: 'put all New Leads into Speed to Lead', 'assign everyone tagged cold to re-engagement'.",
            "parameters": {"type": "object", "properties": {
                "workflow_name": {"type": "string"},
                "pipeline_name": {"type": "string", "description": "Filter by pipeline name"},
                "stage_name": {"type": "string", "description": "Filter by stage"},
                "tag": {"type": "string", "description": "Filter by tag"},
            }, "required": ["workflow_name"]}
        }},
        {"type": "function", "function": {
            "name": "toggle_workflow",
            "description": "Activate or pause a workflow. Use for: 'turn on Speed to Lead', 'pause the re-engagement workflow', 'activate all workflows'.",
            "parameters": {"type": "object", "properties": {
                "workflow_name": {"type": "string"},
                "action": {"type": "string", "enum": ["activate", "pause"]},
            }, "required": ["workflow_name", "action"]}
        }},
        {"type": "function", "function": {
            "name": "create_workflow_ai",
            "description": "Create a new workflow from a natural language description using AI. Use for: 'create a workflow that texts new leads after 30 seconds', 'build an automation that calls cold leads every week'.",
            "parameters": {"type": "object", "properties": {
                "description": {"type": "string", "description": "Natural language description of the workflow"},
            }, "required": ["description"]}
        }},

        # ── Config & Settings ──────────────────────────────────────────
        {"type": "function", "function": {
            "name": "get_bot_config",
            "description": "Get current bot configuration: operator name, timezone, calendar, tone, behavior. Use for: 'what are my bot settings?', 'what name is my bot using?'.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }},
        {"type": "function", "function": {
            "name": "update_bot_config",
            "description": "Update bot settings: operator name, timezone, calendar, tone. Use for: 'change my bot name to Mike', 'set timezone to Eastern', 'make the bot more aggressive'.",
            "parameters": {"type": "object", "properties": {
                "operator_name": {"type": "string"}, "timezone": {"type": "string"},
                "calendar_id": {"type": "string"}, "tone": {"type": "string", "enum": ["friendly", "professional", "aggressive", "casual"]},
            }, "required": []}
        }},
        {"type": "function", "function": {
            "name": "set_carriers",
            "description": "Set the contracted insurance carriers. Use for: 'add Mutual of Omaha to my carriers', 'set my carriers to Americo and National Life', 'update carriers'.",
            "parameters": {"type": "object", "properties": {
                "carriers": {"type": "array", "items": {"type": "string"}, "description": "List of carrier names"},
            }, "required": ["carriers"]}
        }},
        {"type": "function", "function": {
            "name": "list_carriers",
            "description": "Show current contracted carriers. Use for: 'what carriers do I have?', 'my carriers list'.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }},

        # ── Phone Numbers & Health ─────────────────────────────────────
        {"type": "function", "function": {
            "name": "list_phone_numbers",
            "description": "List all phone numbers on the account with health status. Use for: 'show my numbers', 'phone number health', 'which numbers are active?'.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }},

        # ── Billing & Account ──────────────────────────────────────────
        {"type": "function", "function": {
            "name": "get_subscription_info",
            "description": "Get current subscription plan, billing status, and features. Use for: 'what plan am I on?', 'billing info', 'my subscription'.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }},
        {"type": "function", "function": {
            "name": "get_ai_minutes_balance",
            "description": "Check AI minutes balance and usage. Use for: 'how many AI minutes do I have?', 'minutes balance', 'am I running low?'.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }},

        # ── Inbox & Conversations ──────────────────────────────────────
        {"type": "function", "function": {
            "name": "get_inbox_conversations",
            "description": "List recent conversations from the unified inbox. Use for: 'show my inbox', 'recent conversations', 'who messaged me?'.",
            "parameters": {"type": "object", "properties": {
                "limit": {"type": "integer", "description": "Max conversations (default 10)"},
            }, "required": []}
        }},

        # ── Reminders ─────────────────────────────────────────────────
        {"type": "function", "function": {
            "name": "set_reminder",
            "description": "Set a reminder to do something later. Creates a scheduled task. Use for: 'remind me to call John in 2 hours', 'remind me tomorrow at 9am to follow up with Jane', 'set a reminder for Friday'.",
            "parameters": {"type": "object", "properties": {
                "message": {"type": "string", "description": "What to be reminded about"},
                "delay_minutes": {"type": "integer", "description": "Minutes from now (e.g. 120 for 2 hours, 1440 for tomorrow)"},
                "contact_id": {"type": "string", "description": "Optional: contact this reminder is about"},
            }, "required": ["message", "delay_minutes"]}
        }},
        {"type": "function", "function": {
            "name": "list_reminders",
            "description": "Show pending reminders. Use for: 'what reminders do I have?', 'show my reminders'.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }},

        # ── Team Management ────────────────────────────────────────────
        {"type": "function", "function": {
            "name": "list_team_members",
            "description": "List team members with roles and status. Use for: 'show my team', 'who's on my team?', 'team members'.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }},
        {"type": "function", "function": {
            "name": "invite_team_member",
            "description": "Invite a new team member via email. Use for: 'invite john@example.com to my team', 'add a new agent'.",
            "parameters": {"type": "object", "properties": {
                "email": {"type": "string"}, "full_name": {"type": "string"},
                "role": {"type": "string", "enum": ["admin", "agent", "viewer"], "description": "Default: agent"},
            }, "required": ["email"]}
        }},

        # ── Activity Logs ──────────────────────────────────────────────
        {"type": "function", "function": {
            "name": "get_activity_logs",
            "description": "Get recent activity/event logs. Use for: 'show my logs', 'what happened today?', 'recent activity'.",
            "parameters": {"type": "object", "properties": {
                "limit": {"type": "integer", "description": "Max entries (default 15)"},
            }, "required": []}
        }},

        # ── Agency Owner Tools ─────────────────────────────────────────
        {"type": "function", "function": {
            "name": "get_agency_kpis",
            "description": "Get aggregated agency KPIs across all agents. Agency owners only. Use for: 'agency stats', 'how is the team doing?', 'overall performance', 'agency KPIs'.",
            "parameters": {"type": "object", "properties": {
                "period": {"type": "string", "enum": ["today", "week", "month", "all"], "description": "Default: today"},
            }, "required": []}
        }},
        {"type": "function", "function": {
            "name": "get_agent_performance",
            "description": "Get per-agent stats breakdown for agency owners. Use for: 'how is Sarah doing?', 'agent stats', 'who's performing best?', 'compare agents'.",
            "parameters": {"type": "object", "properties": {
                "period": {"type": "string", "enum": ["today", "week", "month", "all"]},
                "agent_name": {"type": "string", "description": "Optional: filter by agent name"},
            }, "required": []}
        }},
        {"type": "function", "function": {
            "name": "get_agency_call_log",
            "description": "Get call log across all agency agents. Use for: 'show all agent calls today', 'agency call log'.",
            "parameters": {"type": "object", "properties": {
                "limit": {"type": "integer", "description": "Default 20"},
                "agent_name": {"type": "string", "description": "Filter by agent"},
            }, "required": []}
        }},
        {"type": "function", "function": {
            "name": "get_agency_leaderboard",
            "description": "Get top performers leaderboard. Agency owners only. Use for: 'who's my top agent?', 'leaderboard', 'best performers', 'rankings'.",
            "parameters": {"type": "object", "properties": {
                "period": {"type": "string", "enum": ["today", "week", "month", "all"]},
                "metric": {"type": "string", "enum": ["connect_rate", "calls", "duration", "messages"], "description": "Rank by which metric. Default: connect_rate"},
            }, "required": []}
        }},
        {"type": "function", "function": {
            "name": "invite_agency_agent",
            "description": "Invite a new agent to the agency. Agency owners only. Use for: 'invite a new agent', 'add agent to agency'.",
            "parameters": {"type": "object", "properties": {
                "email": {"type": "string"}, "full_name": {"type": "string"},
            }, "required": ["email"]}
        }},
        {"type": "function", "function": {
            "name": "list_agency_members",
            "description": "List all agency members and sub-accounts. Agency owners only. Use for: 'show my agents', 'agency members', 'who's in my agency?'.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }},
    ]


# ── Tool execution ────────────────────────────────────────────────────────────

def execute_assistant_tool(tool_name: str, tool_args: dict, user_ctx: dict) -> dict:
    """Execute a tool and return the result dict."""
    try:
        handler = _TOOL_HANDLERS.get(tool_name)
        if not handler:
            return {"error": f"Unknown tool: {tool_name}"}
        result = handler(tool_args, user_ctx)
        return _scrub(result)
    except Exception as e:
        logger.error(f"Assistant tool {tool_name} failed: {e}", exc_info=True)
        return {"error": f"Tool failed: {str(e)[:200]}"}


# ── Individual tool handlers ──────────────────────────────────────────────────

def _handle_search_contact(args, ctx):
    """Search for a contact by name or phone."""
    from contact_validator import search_contact_by_name, search_contact_by_phone
    from db import get_db_connection, return_db_connection

    query = (args.get("query") or "").strip()
    search_type = args.get("search_type", "name")
    location_id = ctx["location_id"]
    from ghl_api import get_valid_token
    access_token = get_valid_token(location_id) or ctx["access_token"]

    if not query:
        return {"error": "No search query provided"}

    contact_id = None

    if search_type == "phone":
        result = search_contact_by_phone(location_id, query)
        if result:
            contact_id = result.get("contact_id") or result.get("id")
    else:
        contact_id = search_contact_by_name(location_id, query)

    if not contact_id:
        return {"found": False, "message": f"No contact found matching '{query}'"}

    # Fetch full contact details from GHL
    try:
        resp = requests.get(
            f"https://services.leadconnectorhq.com/contacts/{contact_id}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Version": "2021-07-28",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            contact = resp.json().get("contact", {})
            result = {
                "found": True,
                "contact_id": contact_id,
                "name": f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip(),
                "first_name": contact.get("firstName", ""),
                "phone": contact.get("phone", ""),
                "email": contact.get("email", ""),
                "tags": contact.get("tags", []),
            }

            # Check for cached AI intelligence
            conn = None
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT analysis FROM contact_intelligence WHERE contact_id = %s",
                    (contact_id,)
                )
                row = cur.fetchone()
                if row and row[0]:
                    intel = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                    result["temperature"] = intel.get("temperature", "")
                    result["score"] = intel.get("score", "")
                    result["summary"] = intel.get("summary", "")
                cur.close()
            except Exception:
                pass
            finally:
                if conn:
                    return_db_connection(conn)

            return result
        else:
            return {"found": True, "contact_id": contact_id, "name": query, "note": "Found ID but couldn't fetch full details"}
    except Exception as e:
        logger.warning(f"Contact detail fetch failed: {e}")
        return {"found": True, "contact_id": contact_id, "name": query}


def _handle_send_sms(args, ctx):
    """Send an SMS message to a contact."""
    from ghl_message import send_sms_via_ghl

    contact_id = args.get("contact_id", "").strip()
    message = args.get("message", "").strip()

    if not contact_id or not message:
        return {"error": "Both contact_id and message are required"}

    from ghl_api import get_valid_token
    access_token = get_valid_token(ctx["location_id"]) or ctx["access_token"]

    success, fail_reason, http_detail = send_sms_via_ghl(
        contact_id=contact_id,
        message=message,
        access_token=access_token,
        location_id=ctx["location_id"],
    )

    if success:
        return {"sent": True, "message": f"SMS sent successfully"}
    else:
        return {"sent": False, "error": fail_reason or "Failed to send SMS"}


def _handle_check_calendar(args, ctx):
    """Check available calendar slots."""
    from ghl_calendar import consolidated_calendar_op
    from ghl_api import get_valid_token

    if not ctx.get("calendar_id"):
        return {"error": "No calendar configured. Go to Bot Config → Calendar & CRM."}

    access_token = get_valid_token(ctx["location_id"])
    if not access_token:
        return {"error": "CRM connection expired. Reconnect in Bot Config."}

    subscriber_data = {
        "location_id": ctx["location_id"],
        "calendar_id": ctx["calendar_id"],
        "crm_user_id": ctx.get("crm_user_id"),
        "timezone": ctx.get("timezone", "America/Chicago"),
        "access_token": access_token,
    }

    try:
        result = consolidated_calendar_op(
            operation="fetch_slots",
            subscriber_data=subscriber_data,
        )

        if isinstance(result, str) and result.strip():
            return {"slots": result}
        elif isinstance(result, dict):
            return {"slots": json.dumps(result)}
        else:
            logger.warning(f"check_calendar returned unexpected: {type(result)} = {str(result)[:200]}")
            return {"error": "Calendar returned no available slots. Your calendar may have no open times in the next few days."}
    except Exception as e:
        logger.error(f"check_calendar failed: {type(e).__name__}: {e}", exc_info=True)
        return {"error": f"Calendar error: {str(e)[:150]}"}


def _handle_book_appointment(args, ctx):
    """Book an appointment for a contact."""
    from ghl_calendar import consolidated_calendar_op

    contact_id = args.get("contact_id", "").strip()
    selected_time = args.get("selected_time", "").strip()
    first_name = args.get("first_name", "").strip()

    if not contact_id or not selected_time:
        return {"error": "Both contact_id and selected_time are required"}

    if not ctx.get("calendar_id"):
        return {"error": "No calendar configured. Go to Bot Config to set one up."}

    from ghl_api import get_valid_token
    access_token = get_valid_token(ctx["location_id"]) or ctx["access_token"]

    subscriber_data = {
        "location_id": ctx["location_id"],
        "calendar_id": ctx["calendar_id"],
        "crm_user_id": ctx.get("crm_user_id"),
        "timezone": ctx.get("timezone", "America/Chicago"),
        "access_token": access_token,
    }

    result = consolidated_calendar_op(
        operation="book",
        subscriber_data=subscriber_data,
        contact_id=contact_id,
        first_name=first_name,
        selected_time=selected_time,
    )

    if result is True or (isinstance(result, dict) and result.get("success")):
        return {"booked": True, "time": selected_time, "contact_id": contact_id}
    elif isinstance(result, str) and "booked" in result.lower():
        return {"booked": True, "time": selected_time, "details": result}
    else:
        return {"booked": False, "error": str(result) if result else "Booking failed"}


def _handle_get_call_stats(args, ctx):
    """Get call statistics for the user."""
    from db import get_db_connection, return_db_connection

    period = args.get("period", "today")
    location_id = ctx["location_id"]

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Build date filter
        if period == "today":
            date_filter = "AND created_at >= CURRENT_DATE"
        elif period == "week":
            date_filter = "AND created_at >= CURRENT_DATE - INTERVAL '7 days'"
        elif period == "month":
            date_filter = "AND created_at >= CURRENT_DATE - INTERVAL '30 days'"
        else:
            date_filter = ""

        cur.execute(f"""
            SELECT
                COUNT(*) as total_calls,
                COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) as connected,
                COALESCE(SUM(duration) FILTER (WHERE status = 'completed'), 0) as total_seconds
            FROM call_history
            WHERE location_id = %s {date_filter}
        """, (location_id,))

        row = cur.fetchone()
        cur.close()

        total = row[0] or 0
        connected = row[1] or 0
        total_seconds = row[2] or 0

        rate = round((connected / total * 100), 1) if total > 0 else 0
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        return {
            "period": period,
            "total_calls": total,
            "connected": connected,
            "connect_rate": f"{rate}%",
            "talk_time": f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m",
        }

    except Exception as e:
        logger.error(f"Call stats query failed: {e}")
        return {"error": "Could not fetch call statistics"}
    finally:
        if conn:
            return_db_connection(conn)


def _handle_navigate_dashboard(args, ctx):
    """Map destination to tab ID."""
    dest = (args.get("destination") or "").lower().strip()

    _NAV_MAP = {
        "dialer": "voicedialer", "phone": "voicedialer", "calls": "voicedialer", "dial": "voicedialer",
        "bot config": "config", "sms settings": "config", "sms config": "config", "bot settings": "config", "sms": "config",
        "voice config": "voice", "voice settings": "voice", "voice": "voice",
        "workflows": "workflows", "automations": "workflows", "automation": "workflows",
        "connect crm": "connect", "integrations": "connect", "connect": "connect", "crm": "connect",
        "carriers": "carriers", "carrier": "carriers",
        "advanced": "advanced", "advanced settings": "advanced",
        "ai minutes": "aiminutes", "minutes": "aiminutes",
        "billing": "billing", "subscription": "billing", "plan": "billing", "payment": "billing",
        "logs": "logs", "activity": "logs", "activity logs": "logs",
        "team": "team", "members": "team",
        "training": "training",
        "white label": "whitelabel", "whitelabel": "whitelabel", "branding": "whitelabel",
    }

    tab_id = _NAV_MAP.get(dest)
    if not tab_id:
        # Fuzzy match: check if destination contains any key
        for key, tid in _NAV_MAP.items():
            if key in dest or dest in key:
                tab_id = tid
                break

    if tab_id:
        return {"action": "navigate", "tab_id": tab_id, "message": f"Navigating to {dest}"}
    else:
        return {"error": f"I don't recognize '{dest}'. Try: dialer, voice settings, billing, workflows, team, logs, or carriers."}


def _handle_get_contact_intelligence(args, ctx):
    """Get cached AI intelligence for a contact."""
    from db import get_db_connection, return_db_connection

    contact_id = args.get("contact_id", "").strip()
    if not contact_id:
        return {"error": "contact_id is required"}

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT analysis, analyzed_at FROM contact_intelligence WHERE contact_id = %s",
            (contact_id,)
        )
        row = cur.fetchone()
        cur.close()

        if not row or not row[0]:
            return {"has_intelligence": False, "message": "No AI analysis available yet for this contact. They'll be analyzed automatically on the next dialer load."}

        intel = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return {
            "has_intelligence": True,
            "temperature": intel.get("temperature", "unknown"),
            "temperature_reason": intel.get("temperature_reason", ""),
            "score": intel.get("score", 0),
            "summary": intel.get("summary", ""),
            "should_respond": intel.get("should_respond", False),
            "should_respond_reason": intel.get("should_respond_reason", ""),
            "actions": intel.get("actions", []),
            "analyzed_at": str(row[1]) if row[1] else "",
        }

    except Exception as e:
        logger.error(f"Intelligence lookup failed: {e}")
        return {"error": "Could not fetch intelligence data"}
    finally:
        if conn:
            return_db_connection(conn)


def _handle_make_call(args, ctx):
    """Initiate an outbound call — returns action for frontend to show call mode choice."""
    contact_id = args.get("contact_id", "").strip()
    phone = args.get("phone", "").strip()
    first_name = args.get("first_name", "").strip() or "there"
    dial_mode = args.get("dial_mode", "").strip().lower()

    if not contact_id or not phone:
        return {"error": "Both contact_id and phone are required. Search for the contact first."}

    # If no dial_mode specified, ask the user
    if not dial_mode:
        return {
            "action": "ask_call_mode",
            "contact_id": contact_id,
            "phone": phone,
            "first_name": first_name,
            "message": f"Do you want AI to call {first_name}, or do you want to talk to them yourself?",
        }

    return {
        "action": "call",
        "contact_id": contact_id,
        "phone": phone,
        "first_name": first_name,
        "dial_mode": dial_mode,
        "message": f"Calling {first_name} now...",
    }


def _handle_queue_dial_session(args, ctx):
    """Fetch contacts by pipeline/stage and return them for the dialer queue."""
    pipeline_name = (args.get("pipeline_name") or "").strip()
    stage_name = (args.get("stage_name") or "").strip()
    dial_mode = args.get("dial_mode", "ai")

    if not pipeline_name:
        return {"error": "Pipeline name is required. Ask which pipeline to dial."}

    location_id = ctx["location_id"]
    from ghl_api import get_valid_token
    access_token = get_valid_token(location_id) or ctx.get("access_token")

    if not access_token:
        return {"error": "No CRM connection. Connect your CRM first."}

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-07-28",
    }

    # Step 1: Fetch pipelines to find matching pipeline_id and stage_id
    try:
        resp = requests.get(
            "https://services.leadconnectorhq.com/opportunities/pipelines",
            headers=headers,
            params={"locationId": location_id},
            timeout=10,
        )
        if resp.status_code != 200:
            return {"error": "Could not fetch pipelines from your CRM."}

        pipelines = resp.json().get("pipelines", [])
    except Exception as e:
        logger.error(f"Pipeline fetch failed: {e}")
        return {"error": "Could not connect to your CRM."}

    # Match pipeline by name (case-insensitive fuzzy)
    pipeline = None
    pl_lower = pipeline_name.lower()
    for p in pipelines:
        if p.get("name", "").lower() == pl_lower:
            pipeline = p
            break
    if not pipeline:
        for p in pipelines:
            if pl_lower in p.get("name", "").lower() or p.get("name", "").lower() in pl_lower:
                pipeline = p
                break
    if not pipeline:
        available = [p.get("name") for p in pipelines]
        return {"error": f"No pipeline matching '{pipeline_name}'. Available: {', '.join(available)}"}

    pipeline_id = pipeline["id"]

    # Match stage if specified
    stage_id = None
    if stage_name:
        st_lower = stage_name.lower()
        for s in pipeline.get("stages", []):
            if s.get("name", "").lower() == st_lower:
                stage_id = s["id"]
                break
        if not stage_id:
            for s in pipeline.get("stages", []):
                if st_lower in s.get("name", "").lower() or s.get("name", "").lower() in st_lower:
                    stage_id = s["id"]
                    break
        if not stage_id:
            available = [s.get("name") for s in pipeline.get("stages", [])]
            return {"error": f"No stage matching '{stage_name}' in {pipeline.get('name')}. Available: {', '.join(available)}"}

    # Step 2: Fetch contacts in this pipeline/stage via opportunities API
    try:
        opp_params = {"location_id": location_id, "pipeline_id": pipeline_id, "limit": "100"}
        if stage_id:
            opp_params["pipeline_stage_id"] = stage_id

        opp_resp = requests.get(
            "https://services.leadconnectorhq.com/opportunities/search",
            headers=headers,
            params=opp_params,
            timeout=15,
        )
        if opp_resp.status_code != 200:
            return {"error": "Could not fetch contacts from pipeline."}

        opportunities = opp_resp.json().get("opportunities", [])
    except Exception as e:
        logger.error(f"Opportunities fetch failed: {e}")
        return {"error": "Could not fetch pipeline contacts."}

    if not opportunities:
        stage_label = f" / {stage_name}" if stage_name else ""
        return {"error": f"No contacts found in {pipeline.get('name')}{stage_label}."}

    # Build contact list for the queue
    contacts = []
    for opp in opportunities:
        contact = opp.get("contact", {})
        contact_id = contact.get("id") or opp.get("contactId", "")
        name = f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip()
        phone = contact.get("phone", "")
        first_name = contact.get("firstName", "")

        if contact_id and phone:
            contacts.append({
                "id": contact_id,
                "name": name or "Unknown",
                "firstName": first_name or name.split()[0] if name else "there",
                "phone": phone,
            })

    if not contacts:
        return {"error": "Found opportunities but none have phone numbers."}

    stage_label = f" / {stage_name}" if stage_name else ""
    return {
        "action": "dial_queue",
        "contacts": contacts,
        "dial_mode": dial_mode,
        "pipeline_name": pipeline.get("name"),
        "stage_name": stage_name,
        "message": f"Queued {len(contacts)} contacts from {pipeline.get('name')}{stage_label}. Ready to start dialing.",
    }


def _handle_query_call_history(args, ctx):
    """Query call history with flexible filters."""
    from db import get_db_connection, return_db_connection

    period = args.get("period", "today")
    direction = args.get("direction", "all")
    min_dur = args.get("min_duration_seconds", 0)
    status = args.get("status", "all")
    has_rec = args.get("has_recording", False)
    sort = args.get("sort", "recent")
    limit = min(args.get("limit", 10), 50)

    location_id = ctx["location_id"]

    # Build date filter
    date_filters = {
        "today": "AND ch.created_at >= CURRENT_DATE",
        "yesterday": "AND ch.created_at >= CURRENT_DATE - INTERVAL '1 day' AND ch.created_at < CURRENT_DATE",
        "week": "AND ch.created_at >= CURRENT_DATE - INTERVAL '7 days'",
        "month": "AND ch.created_at >= CURRENT_DATE - INTERVAL '30 days'",
        "all": "",
    }
    date_sql = date_filters.get(period, "")

    wheres = [f"ch.location_id = %s {date_sql}"]
    params = [location_id]

    if direction != "all":
        wheres.append("ch.direction = %s")
        params.append(direction)
    if min_dur > 0:
        wheres.append("ch.duration >= %s")
        params.append(min_dur)
    if status != "all":
        wheres.append("ch.status = %s")
        params.append(status)
    if has_rec:
        wheres.append("ch.recording_url IS NOT NULL AND ch.recording_url != ''")

    sort_map = {
        "duration_desc": "ch.duration DESC NULLS LAST",
        "duration_asc": "ch.duration ASC NULLS LAST",
        "recent": "ch.created_at DESC",
        "oldest": "ch.created_at ASC",
    }
    order = sort_map.get(sort, "ch.created_at DESC")

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT ch.contact_name, ch.phone_number, ch.direction, ch.status,
                   ch.duration, ch.created_at, ch.contact_id,
                   CASE WHEN ch.recording_url IS NOT NULL AND ch.recording_url != '' THEN true ELSE false END as has_recording,
                   CASE WHEN ch.transcript IS NOT NULL AND ch.transcript != '' THEN true ELSE false END as has_transcript
            FROM call_history ch
            WHERE {' AND '.join(wheres)}
            ORDER BY {order}
            LIMIT %s
        """, params + [limit])

        rows = cur.fetchall()
        cur.close()

        calls = []
        for r in rows:
            dur = r[4] or 0
            mins = dur // 60
            secs = dur % 60
            calls.append({
                "name": r[0] or "Unknown",
                "phone": r[1] or "",
                "direction": r[2] or "outbound",
                "status": r[3] or "unknown",
                "duration": f"{mins}m {secs}s" if mins > 0 else f"{secs}s",
                "duration_seconds": dur,
                "time": str(r[5])[:16] if r[5] else "",
                "contact_id": r[6] or "",
                "has_recording": r[7],
                "has_transcript": r[8],
            })

        if not calls:
            return {"count": 0, "message": "No calls found matching your criteria."}

        return {"count": len(calls), "calls": calls}

    except Exception as e:
        logger.error(f"Call history query failed: {e}")
        return {"error": "Could not query call history."}
    finally:
        if conn:
            return_db_connection(conn)


def _handle_get_hot_leads(args, ctx):
    """Get leads ranked by AI intelligence score."""
    from db import get_db_connection, return_db_connection

    temperature = args.get("temperature", "hot")
    limit = min(args.get("limit", 10), 30)
    respond_only = args.get("should_respond_only", False)

    location_id = ctx["location_id"]

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Join contact_intelligence with contact_cache for names/phones
        wheres = ["cc.location_id = %s", "ci.analysis IS NOT NULL"]
        params = [location_id]

        if temperature != "all":
            wheres.append("ci.analysis->>'temperature' = %s")
            params.append(temperature)

        if respond_only:
            wheres.append("(ci.analysis->>'should_respond')::boolean = true")

        cur.execute(f"""
            SELECT cc.contact_id, cc.first_name, cc.last_name, cc.phone, cc.email,
                   ci.analysis->>'temperature' as temperature,
                   (ci.analysis->>'score')::int as score,
                   ci.analysis->>'summary' as summary,
                   (ci.analysis->>'should_respond')::boolean as should_respond,
                   ci.analysis->>'should_respond_reason' as respond_reason
            FROM contact_cache cc
            JOIN contact_intelligence ci ON ci.contact_id = cc.contact_id
            WHERE {' AND '.join(wheres)}
            ORDER BY (ci.analysis->>'score')::int DESC NULLS LAST
            LIMIT %s
        """, params + [limit])

        rows = cur.fetchall()
        cur.close()

        leads = []
        for r in rows:
            leads.append({
                "contact_id": r[0],
                "name": f"{r[1] or ''} {r[2] or ''}".strip() or "Unknown",
                "phone": r[3] or "",
                "email": r[4] or "",
                "temperature": r[5] or "unknown",
                "score": r[6] or 0,
                "summary": r[7] or "",
                "should_respond": r[8],
                "respond_reason": r[9] or "",
            })

        if not leads:
            return {"count": 0, "message": f"No {temperature} leads found."}

        return {"count": len(leads), "leads": leads}

    except Exception as e:
        logger.error(f"Hot leads query failed: {e}")
        return {"error": "Could not fetch leads."}
    finally:
        if conn:
            return_db_connection(conn)


def _handle_get_upcoming_appointments(args, ctx):
    """Fetch upcoming appointments from GHL calendar."""
    days_ahead = min(args.get("days_ahead", 1), 7)
    location_id = ctx["location_id"]
    cal_id = ctx.get("calendar_id")
    tz_str = ctx.get("timezone", "America/Chicago")

    if not cal_id:
        return {"error": "No calendar configured. Set one in Bot Config → Calendar."}

    # Use get_valid_token for proper decryption + refresh
    from ghl_api import get_valid_token
    access_token = get_valid_token(location_id)
    if not access_token:
        return {"error": "CRM connection expired. Reconnect in Bot Config."}

    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    local_tz = ZoneInfo(tz_str)
    now = datetime.now(local_tz)
    start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = start_dt + timedelta(days=days_ahead, hours=23, minutes=59)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-04-15",
    }

    # Try millisecond timestamps first (GHL primary format per docs)
    start_ms = int(start_dt.astimezone(timezone.utc).timestamp() * 1000)
    end_ms = int(end_dt.astimezone(timezone.utc).timestamp() * 1000)

    params = {
        "locationId": location_id,
        "calendarId": cal_id,
        "startTime": str(start_ms),
        "endTime": str(end_ms),
    }

    try:
        resp = requests.get(
            "https://services.leadconnectorhq.com/calendars/events",
            headers=headers,
            params=params,
            timeout=15,
        )
        logger.info(f"Calendar events response: {resp.status_code} for {location_id} cal={cal_id}")

        # If millisecond format fails, retry with ISO string format
        if resp.status_code == 400:
            logger.info("Calendar: retrying with ISO datetime format")
            headers["Version"] = "2021-07-28"
            params["startTime"] = start_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            params["endTime"] = end_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            resp = requests.get(
                "https://services.leadconnectorhq.com/calendars/events",
                headers=headers,
                params=params,
                timeout=15,
            )
            logger.info(f"Calendar events retry: {resp.status_code}")

        if resp.status_code in (401, 403):
            return {"error": "CRM connection expired. Reconnect in Bot Config."}
        if resp.status_code != 200:
            logger.warning(f"Calendar events {resp.status_code}: {resp.text[:300]}")
            return {"error": f"Calendar API returned {resp.status_code}. Check your calendar configuration."}

        data = resp.json()
        raw_events = data.get("events", [])
        if not raw_events and isinstance(data, list):
            raw_events = data

        if not raw_events:
            return {"count": 0, "message": f"No appointments in the next {days_ahead} day{'s' if days_ahead > 1 else ''}."}

        appointments = []
        for ev in raw_events[:20]:
            start_time = ev.get("startTime") or ev.get("start") or ""
            # Format for display
            display_time = ""
            if start_time:
                try:
                    if start_time.endswith("Z"):
                        start_time = start_time.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(start_time).astimezone(local_tz)
                    display_time = dt.strftime("%a %b %d, %I:%M %p")
                except Exception:
                    display_time = start_time[:16]

            appointments.append({
                "title": ev.get("title") or "Appointment",
                "time": display_time,
                "contact_name": ev.get("contactId", ""),
                "status": ev.get("appointmentStatus") or ev.get("status") or "",
            })

        return {"count": len(appointments), "appointments": appointments}

    except Exception as e:
        logger.error(f"Calendar events fetch failed: {type(e).__name__}: {e}", exc_info=True)
        return {"error": f"Calendar error: {type(e).__name__}: {str(e)[:150]}"}


def _handle_get_recent_messages(args, ctx):
    """Get recent SMS messages."""
    from db import get_db_connection, return_db_connection

    direction = args.get("direction", "inbound")
    period = args.get("period", "today")
    limit = min(args.get("limit", 10), 30)
    location_id = ctx["location_id"]

    date_filters = {
        "today": "AND cm.created_at >= CURRENT_DATE",
        "yesterday": "AND cm.created_at >= CURRENT_DATE - INTERVAL '1 day' AND cm.created_at < CURRENT_DATE",
        "week": "AND cm.created_at >= CURRENT_DATE - INTERVAL '7 days'",
    }
    date_sql = date_filters.get(period, "AND cm.created_at >= CURRENT_DATE")

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        dir_filter = ""
        if direction == "inbound":
            dir_filter = "AND cm.direction = 'inbound'"
        elif direction == "outbound":
            dir_filter = "AND cm.direction = 'outbound'"

        cur.execute(f"""
            SELECT cm.contact_id, cm.message, cm.direction, cm.created_at,
                   cc.first_name, cc.last_name, cc.phone
            FROM contact_messages cm
            LEFT JOIN contact_cache cc ON cc.contact_id = cm.contact_id AND cc.location_id = %s
            WHERE cm.location_id = %s {date_sql} {dir_filter}
            ORDER BY cm.created_at DESC
            LIMIT %s
        """, (location_id, location_id, limit))

        rows = cur.fetchall()
        cur.close()

        messages = []
        for r in rows:
            messages.append({
                "contact_id": r[0] or "",
                "message": (r[1] or "")[:150],
                "direction": r[2] or "inbound",
                "time": str(r[3])[:16] if r[3] else "",
                "name": f"{r[4] or ''} {r[5] or ''}".strip() or "Unknown",
                "phone": r[6] or "",
            })

        if not messages:
            dir_label = {"inbound": "inbound", "outbound": "outbound", "all": ""}
            return {"count": 0, "message": f"No {dir_label.get(direction, '')} messages found for {period}."}

        return {"count": len(messages), "messages": messages}

    except Exception as e:
        logger.error(f"Messages query failed: {e}")
        return {"error": "Could not fetch messages."}
    finally:
        if conn:
            return_db_connection(conn)


def _handle_get_stale_leads(args, ctx):
    """Find leads that haven't been contacted recently."""
    from db import get_db_connection, return_db_connection

    days = args.get("days_since_contact", 7)
    limit = min(args.get("limit", 15), 50)
    location_id = ctx["location_id"]

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Find contacts in cache that have NO recent call or message
        # Use make_interval() for safe parameterized interval
        cur.execute("""
            SELECT cc.contact_id, cc.first_name, cc.last_name, cc.phone, cc.email,
                   ci.analysis->>'temperature' as temperature,
                   (ci.analysis->>'score')::int as score,
                   ci.analysis->>'summary' as summary,
                   GREATEST(
                       (SELECT MAX(created_at) FROM call_history WHERE contact_id = cc.contact_id),
                       (SELECT MAX(created_at) FROM contact_messages WHERE contact_id = cc.contact_id)
                   ) as last_contact
            FROM contact_cache cc
            LEFT JOIN contact_intelligence ci ON ci.contact_id = cc.contact_id
            WHERE cc.location_id = %s
              AND cc.phone IS NOT NULL AND cc.phone != ''
              AND NOT EXISTS (
                  SELECT 1 FROM call_history ch
                  WHERE ch.contact_id = cc.contact_id
                    AND ch.created_at >= CURRENT_DATE - make_interval(days => %s)
              )
              AND NOT EXISTS (
                  SELECT 1 FROM contact_messages cm
                  WHERE cm.contact_id = cc.contact_id
                    AND cm.created_at >= CURRENT_DATE - make_interval(days => %s)
              )
            ORDER BY last_contact ASC NULLS FIRST
            LIMIT %s
        """, (location_id, days, days, limit))

        rows = cur.fetchall()
        cur.close()

        leads = []
        for r in rows:
            leads.append({
                "contact_id": r[0],
                "name": f"{r[1] or ''} {r[2] or ''}".strip() or "Unknown",
                "phone": r[3] or "",
                "email": r[4] or "",
                "temperature": r[5] or "unknown",
                "score": r[6] or 0,
                "summary": r[7] or "",
                "last_contact": str(r[8])[:10] if r[8] else "Never",
            })

        if not leads:
            return {"count": 0, "message": f"All your leads have been contacted within the last {days} days. Nice work!"}

        return {"count": len(leads), "leads": leads}

    except Exception as e:
        logger.error(f"Stale leads query failed: {e}")
        return {"error": "Could not fetch stale leads."}
    finally:
        if conn:
            return_db_connection(conn)


# ══════════════════════════════════════════════════════════════════════════════
# ADDITIONAL QUERY HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

def _handle_get_pipeline_summary(args, ctx):
    h = _ghl_headers(ctx)
    if not h: return {"error": "CRM connection expired"}
    try:
        r = requests.get("https://services.leadconnectorhq.com/opportunities/pipelines", headers=h, params={"locationId": ctx["location_id"]}, timeout=10)
        if r.status_code != 200: return {"error": "Could not fetch pipelines"}
        pipelines = r.json().get("pipelines", [])
        pn = (args.get("pipeline_name") or "").lower()
        result = []
        for p in pipelines:
            if pn and pn not in p.get("name", "").lower(): continue
            stages = [{"name": s.get("name", ""), "position": s.get("position", 0)} for s in p.get("stages", [])]
            result.append({"name": p.get("name", ""), "id": p.get("id", ""), "stages": stages})
        return {"pipelines": result} if result else {"message": "No pipelines found"}
    except Exception as e: return {"error": str(e)[:200]}

def _handle_search_recordings(args, ctx):
    from db import get_db_connection, return_db_connection
    contact_name = args.get("contact_name", "")
    period = args.get("period", "week")
    limit = min(args.get("limit", 10), 30)
    date_filters = {"today": "AND created_at >= CURRENT_DATE", "yesterday": "AND created_at >= CURRENT_DATE - INTERVAL '1 day' AND created_at < CURRENT_DATE", "week": "AND created_at >= CURRENT_DATE - INTERVAL '7 days'", "month": "AND created_at >= CURRENT_DATE - INTERVAL '30 days'"}
    df = date_filters.get(period, "AND created_at >= CURRENT_DATE - INTERVAL '7 days'")
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        name_filter = "AND LOWER(contact_name) LIKE %s" if contact_name else ""
        params = [ctx["location_id"]]
        if contact_name: params.append(f"%{contact_name.lower()}%")
        cur.execute(f"SELECT contact_name, phone_number, duration, created_at, recording_url, transcript IS NOT NULL as has_transcript FROM call_history WHERE location_id = %s AND recording_url IS NOT NULL AND recording_url != '' {name_filter} {df} ORDER BY created_at DESC LIMIT %s", params + [limit])
        recs = [{"name": r[0] or "Unknown", "phone": r[1] or "", "duration": f"{(r[2] or 0)//60}m {(r[2] or 0)%60}s", "time": str(r[3])[:16] if r[3] else "", "has_transcript": r[5]} for r in cur.fetchall()]
        cur.close()
        return {"count": len(recs), "recordings": recs} if recs else {"count": 0, "message": "No recordings found."}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)

def _handle_send_bulk_sms(args, ctx):
    message = args.get("message", "").strip()
    if not message: return {"error": "Message text required"}
    pipeline_name = args.get("pipeline_name", "")
    stage_name = args.get("stage_name", "")
    limit = min(args.get("limit", 25), 50)
    if not pipeline_name and not args.get("tag"): return {"error": "Specify pipeline_name or tag to select recipients"}
    # Get contacts
    contacts_result = _handle_queue_dial_session({"pipeline_name": pipeline_name, "stage_name": stage_name}, ctx)
    if "error" in contacts_result: return contacts_result
    contacts = contacts_result.get("contacts", [])[:limit]
    if not contacts: return {"error": "No contacts found matching criteria"}
    # Send to each
    from ghl_message import send_sms_via_ghl
    from ghl_api import get_valid_token
    access_token = get_valid_token(ctx["location_id"]) or ctx.get("access_token", "")
    sent = 0
    failed = 0
    for c in contacts:
        try:
            ok, _, _ = send_sms_via_ghl(c["id"], message, access_token, ctx["location_id"])
            if ok: sent += 1
            else: failed += 1
        except Exception:
            failed += 1
    return {"sent": sent, "failed": failed, "total": len(contacts), "message": f"Sent to {sent}/{len(contacts)} contacts"}

def _handle_get_daily_summary(args, ctx):
    period = args.get("period", "today")
    df = "AND created_at >= CURRENT_DATE" if period == "today" else "AND created_at >= CURRENT_DATE - INTERVAL '1 day' AND created_at < CURRENT_DATE"
    from db import get_db_connection, return_db_connection
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        lid = ctx["location_id"]
        # Calls
        cur.execute(f"SELECT COUNT(*), COUNT(*) FILTER (WHERE status='completed' AND duration>0), COALESCE(SUM(duration) FILTER (WHERE status='completed'),0), COUNT(*) FILTER (WHERE direction='inbound' AND status='no-answer') FROM call_history WHERE location_id=%s {df}", (lid,))
        cr = cur.fetchone()
        # Messages
        cur.execute(f"SELECT COUNT(*) FILTER (WHERE direction='outbound'), COUNT(*) FILTER (WHERE direction='inbound') FROM contact_messages WHERE location_id=%s {df}", (lid,))
        mr = cur.fetchone()
        # Hot leads
        cur.execute("SELECT COUNT(*) FROM contact_intelligence ci JOIN contact_cache cc ON cc.contact_id = ci.contact_id AND cc.location_id = %s WHERE ci.analysis->>'temperature' = 'hot'", (lid,))
        hot = cur.fetchone()[0] or 0
        cur.close()
        total_calls = cr[0] or 0
        connected = cr[1] or 0
        talk_secs = cr[2] or 0
        missed = cr[3] or 0
        return {
            "period": period,
            "calls_made": total_calls, "calls_connected": connected,
            "connect_rate": f"{round(connected/total_calls*100,1)}%" if total_calls > 0 else "0%",
            "talk_time": f"{talk_secs//3600}h {(talk_secs%3600)//60}m" if talk_secs >= 3600 else f"{talk_secs//60}m",
            "missed_calls": missed,
            "texts_sent": mr[0] or 0, "texts_received": mr[1] or 0,
            "hot_leads": hot,
        }
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)


# ══════════════════════════════════════════════════════════════════════════════
# CONTACT MANAGEMENT HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

def _ghl_headers(ctx):
    from ghl_api import get_valid_token
    tok = get_valid_token(ctx["location_id"])
    return {"Authorization": f"Bearer {tok}", "Version": "2021-07-28", "Content-Type": "application/json"} if tok else None

def _handle_edit_contact(args, ctx):
    cid = args.pop("contact_id", "")
    if not cid: return {"error": "contact_id required"}
    h = _ghl_headers(ctx)
    if not h: return {"error": "CRM connection expired"}
    body = {k: v for k, v in args.items() if v}
    if not body: return {"error": "No fields to update"}
    try:
        r = requests.put(f"https://services.leadconnectorhq.com/contacts/{cid}", headers=h, json=body, timeout=10)
        return {"updated": r.status_code == 200, "fields": list(body.keys())} if r.status_code == 200 else {"error": f"Update failed ({r.status_code})"}
    except Exception as e: return {"error": str(e)[:200]}

def _handle_add_contact_tag(args, ctx):
    cid = args.get("contact_id", "")
    tags = args.get("tags", [])
    if not cid or not tags: return {"error": "contact_id and tags required"}
    h = _ghl_headers(ctx)
    if not h: return {"error": "CRM connection expired"}
    try:
        r = requests.post(f"https://services.leadconnectorhq.com/contacts/{cid}/tags", headers=h, json={"tags": tags}, timeout=10)
        return {"added": True, "tags": tags} if r.status_code in (200, 201) else {"error": f"Failed ({r.status_code})"}
    except Exception as e: return {"error": str(e)[:200]}

def _handle_remove_contact_tag(args, ctx):
    cid = args.get("contact_id", "")
    tag = args.get("tag", "")
    if not cid or not tag: return {"error": "contact_id and tag required"}
    h = _ghl_headers(ctx)
    if not h: return {"error": "CRM connection expired"}
    try:
        r = requests.delete(f"https://services.leadconnectorhq.com/contacts/{cid}/tags", headers=h, json={"tags": [tag]}, timeout=10)
        return {"removed": True, "tag": tag} if r.status_code in (200, 204) else {"error": f"Failed ({r.status_code})"}
    except Exception as e: return {"error": str(e)[:200]}

def _handle_add_contact_note(args, ctx):
    cid = args.get("contact_id", "")
    note = args.get("note", "")
    if not cid or not note: return {"error": "contact_id and note required"}
    h = _ghl_headers(ctx)
    if not h: return {"error": "CRM connection expired"}
    try:
        r = requests.post(f"https://services.leadconnectorhq.com/contacts/{cid}/notes", headers=h, json={"body": note}, timeout=10)
        return {"added": True, "note": note[:100]} if r.status_code in (200, 201) else {"error": f"Failed ({r.status_code})"}
    except Exception as e: return {"error": str(e)[:200]}

def _handle_create_contact(args, ctx):
    h = _ghl_headers(ctx)
    if not h: return {"error": "CRM connection expired"}
    body = {"locationId": ctx["location_id"]}
    for k in ("first_name", "last_name", "phone", "email"):
        v = args.get(k, "").strip()
        if v: body[{"first_name": "firstName", "last_name": "lastName"}.get(k, k)] = v
    if args.get("tags"): body["tags"] = args["tags"]
    try:
        r = requests.post("https://services.leadconnectorhq.com/contacts/", headers=h, json=body, timeout=10)
        if r.status_code in (200, 201):
            data = r.json().get("contact", {})
            return {"created": True, "contact_id": data.get("id", ""), "name": f"{body.get('firstName', '')} {body.get('lastName', '')}".strip()}
        return {"error": f"Failed ({r.status_code}): {r.text[:200]}"}
    except Exception as e: return {"error": str(e)[:200]}

def _handle_move_contact_pipeline(args, ctx):
    cid = args.get("contact_id", "")
    stage_name = args.get("stage_name", "")
    pipeline_name = args.get("pipeline_name", "")
    if not cid or not stage_name: return {"error": "contact_id and stage_name required"}
    h = _ghl_headers(ctx)
    if not h: return {"error": "CRM connection expired"}
    # Fetch pipelines to resolve IDs
    try:
        pr = requests.get("https://services.leadconnectorhq.com/opportunities/pipelines", headers=h, params={"locationId": ctx["location_id"]}, timeout=10)
        if pr.status_code != 200: return {"error": "Could not fetch pipelines"}
        pipelines = pr.json().get("pipelines", [])
        # Find pipeline + stage
        for p in pipelines:
            if pipeline_name and pipeline_name.lower() not in p.get("name", "").lower(): continue
            for s in p.get("stages", []):
                if stage_name.lower() in s.get("name", "").lower():
                    # Find opportunity for this contact
                    opp_r = requests.get("https://services.leadconnectorhq.com/opportunities/search", headers=h,
                                        params={"location_id": ctx["location_id"], "contact_id": cid, "pipeline_id": p["id"]}, timeout=10)
                    opps = opp_r.json().get("opportunities", []) if opp_r.status_code == 200 else []
                    if opps:
                        opp_id = opps[0].get("id")
                        ur = requests.put(f"https://services.leadconnectorhq.com/opportunities/{opp_id}", headers=h,
                                          json={"pipelineStageId": s["id"]}, timeout=10)
                        return {"moved": ur.status_code == 200, "stage": s["name"], "pipeline": p["name"]}
                    return {"error": f"No opportunity found for this contact in {p['name']}"}
        return {"error": f"Stage '{stage_name}' not found"}
    except Exception as e: return {"error": str(e)[:200]}

def _handle_get_contact_history(args, ctx):
    from db import get_db_connection, return_db_connection
    cid = args.get("contact_id", "")
    limit = min(args.get("limit", 20), 50)
    if not cid: return {"error": "contact_id required"}
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT message, direction, created_at FROM contact_messages WHERE contact_id = %s ORDER BY created_at DESC LIMIT %s", (cid, limit))
        msgs = [{"text": r[0][:150] if r[0] else "", "direction": r[1] or "", "time": str(r[2])[:16] if r[2] else ""} for r in cur.fetchall()]
        cur.execute("SELECT contact_name, direction, status, duration, created_at FROM call_history WHERE contact_id = %s ORDER BY created_at DESC LIMIT %s", (cid, limit))
        calls = [{"direction": r[1] or "", "status": r[2] or "", "duration": f"{(r[3] or 0)//60}m {(r[3] or 0)%60}s", "time": str(r[4])[:16] if r[4] else ""} for r in cur.fetchall()]
        cur.close()
        return {"messages": msgs, "calls": calls}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)

def _handle_mark_do_not_contact(args, ctx):
    cid = args.get("contact_id", "")
    if not cid: return {"error": "contact_id required"}
    h = _ghl_headers(ctx)
    if not h: return {"error": "CRM connection expired"}
    try:
        r = requests.post(f"https://services.leadconnectorhq.com/contacts/{cid}/tags", headers=h, json={"tags": ["DNC", "do-not-contact"]}, timeout=10)
        requests.put(f"https://services.leadconnectorhq.com/contacts/{cid}", headers=h, json={"dnd": True, "dndSettings": {"Call": {"status": "inactive"}, "SMS": {"status": "inactive"}}}, timeout=10)
        return {"marked": True, "message": "Contact marked as Do Not Contact"}
    except Exception as e: return {"error": str(e)[:200]}


# ══════════════════════════════════════════════════════════════════════════════
# WORKFLOW HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

def _handle_list_workflows(args, ctx):
    from db import get_db_connection, return_db_connection
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, name, status, trigger_type, created_at FROM workflows WHERE location_id = %s ORDER BY created_at DESC", (ctx["location_id"],))
        wfs = [{"id": str(r[0]), "name": r[1], "status": r[2], "trigger": r[3], "created": str(r[4])[:10] if r[4] else ""} for r in cur.fetchall()]
        cur.close()
        return {"count": len(wfs), "workflows": wfs} if wfs else {"count": 0, "message": "No workflows found."}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)

def _handle_assign_contact_to_workflow(args, ctx):
    from db import get_db_connection, return_db_connection
    import uuid
    from datetime import datetime, timezone

    cid = args.get("contact_id", "")
    wf_name = args.get("workflow_name", "")
    if not cid or not wf_name: return {"error": "contact_id and workflow_name required"}
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM workflows WHERE location_id = %s AND LOWER(name) LIKE %s AND status = 'active' LIMIT 1", (ctx["location_id"], f"%{wf_name.lower()}%"))
        row = cur.fetchone()
        if not row:
            cur.close()
            return {"error": f"No active workflow matching '{wf_name}'"}

        wf_id = row[0]
        wf_real_name = row[1]
        run_id = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()

        cur.execute("""
            INSERT INTO workflow_runs (id, workflow_id, contact_id, status, started_at, context)
            VALUES (%s, %s, %s, 'running', %s, %s)
        """, (run_id, wf_id, cid, now, json.dumps({"triggered_by": "assistant", "email": ctx.get("email", "")})))
        conn.commit()
        cur.close()

        # Enqueue for background execution
        try:
            from extensions import ensure_redis
            r = ensure_redis()
            if r:
                from rq import Queue
                q = Queue("production", connection=r)
                from workflow_engine import execute_workflow_run
                q.enqueue(execute_workflow_run, run_id, job_timeout=120, job_id=f"wf-assist-{run_id[:8]}")
        except Exception as e:
            logger.warning(f"Failed to enqueue workflow run: {e}")

        return {"enrolled": True, "workflow": wf_real_name, "contact_id": cid, "run_id": run_id}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)

def _handle_assign_bulk_to_workflow(args, ctx):
    wf_name = args.get("workflow_name", "")
    pipeline_name = args.get("pipeline_name", "")
    stage_name = args.get("stage_name", "")
    tag = args.get("tag", "")
    if not wf_name: return {"error": "workflow_name required"}
    if not pipeline_name and not tag: return {"error": "Specify pipeline_name or tag to select contacts"}
    # First get the contacts via the queue logic
    if pipeline_name:
        contacts_result = _handle_queue_dial_session({"pipeline_name": pipeline_name, "stage_name": stage_name}, ctx)
        if "error" in contacts_result: return contacts_result
        contact_ids = [c["id"] for c in contacts_result.get("contacts", [])]
    else:
        return {"error": "Tag-based bulk assignment not yet supported. Use pipeline_name."}
    if not contact_ids: return {"error": "No contacts found matching criteria"}
    # Enroll each
    enrolled = 0
    for cid in contact_ids[:100]:
        r = _handle_assign_contact_to_workflow({"contact_id": cid, "workflow_name": wf_name}, ctx)
        if r.get("enrolled"): enrolled += 1
    return {"enrolled": enrolled, "total": len(contact_ids), "workflow": wf_name}

def _handle_toggle_workflow(args, ctx):
    from db import get_db_connection, return_db_connection
    wf_name = args.get("workflow_name", "")
    action = args.get("action", "activate")
    if not wf_name: return {"error": "workflow_name required"}
    new_status = "active" if action == "activate" else "paused"
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE workflows SET status = %s, updated_at = NOW() WHERE location_id = %s AND LOWER(name) LIKE %s RETURNING name, status",
                    (new_status, ctx["location_id"], f"%{wf_name.lower()}%"))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        if row: return {"toggled": True, "workflow": row[0], "status": row[1]}
        return {"error": f"No workflow matching '{wf_name}'"}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)

def _handle_create_workflow_ai(args, ctx):
    desc = args.get("description", "")
    if not desc: return {"error": "Description required"}
    # Delegate to the existing AI workflow builder endpoint
    try:
        from flask import current_app
        with current_app.test_client() as c:
            # We can't easily call another route internally, so build it directly
            pass
    except Exception:
        pass
    return {"action": "navigate", "tab_id": "workflows", "message": f"I've opened the Workflows tab. Use the AI Builder button and describe: \"{desc}\""}


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG & SETTINGS HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

def _handle_get_bot_config(args, ctx):
    from db import get_db_connection, return_db_connection
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT bot_first_name, timezone, calendar_id, bot_settings FROM subscribers WHERE location_id = %s", (ctx["location_id"],))
        row = cur.fetchone()
        cur.close()
        if not row: return {"error": "Account not found"}
        settings = row[3] if isinstance(row[3], dict) else {}
        return {"operator_name": row[0] or "", "timezone": row[1] or "", "calendar_id": row[2] or "", "tone": settings.get("tone", "friendly"), "settings": settings}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)

def _handle_update_bot_config(args, ctx):
    from db import get_db_connection, return_db_connection
    updates = []
    params = []
    if args.get("operator_name"):
        updates.append("bot_first_name = %s")
        params.append(args["operator_name"])
    if args.get("timezone"):
        updates.append("timezone = %s")
        params.append(args["timezone"])
    if args.get("calendar_id"):
        updates.append("calendar_id = %s")
        params.append(args["calendar_id"])
    if not updates: return {"error": "No fields to update"}
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"UPDATE subscribers SET {', '.join(updates)}, updated_at = NOW() WHERE location_id = %s", params + [ctx["location_id"]])
        conn.commit()
        cur.close()
        return {"updated": True, "fields": list(args.keys())}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)

def _handle_set_carriers(args, ctx):
    from db import save_contracted_carriers
    from carrier_list import CARRIER_LIST
    carrier_names = args.get("carriers", [])
    if not carrier_names: return {"error": "No carriers provided"}
    # Match names to keys
    keys = []
    for name in carrier_names:
        nl = name.lower()
        for c in CARRIER_LIST:
            if nl in c["name"].lower() or c["name"].lower() in nl:
                keys.append(c["key"])
                break
    if not keys: return {"error": f"No matching carriers found for: {', '.join(carrier_names)}"}
    ok = save_contracted_carriers(ctx["email"], keys)
    return {"saved": ok, "count": len(keys), "carriers": keys}

def _handle_list_carriers(args, ctx):
    from db import get_contracted_carriers
    selected = get_contracted_carriers(ctx["email"])
    from carrier_list import CARRIER_LIST
    names = [c["name"] for c in CARRIER_LIST if c["key"] in selected]
    return {"count": len(names), "carriers": names} if names else {"count": 0, "message": "No carriers selected yet."}

def _handle_list_phone_numbers(args, ctx):
    from db import get_db_connection, return_db_connection
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE location_id = %s", (ctx["location_id"],))
        row = cur.fetchone()
        cur.close()
        if not row: return {"error": "Account not found"}
        vc = row[0] or {}
        sub_sid = vc.get("twilio_sub_account_sid")
        if not sub_sid: return {"numbers": [], "message": "No phone numbers provisioned yet."}
        import twilio_provisioning
        numbers = twilio_provisioning.list_phone_numbers(sub_sid)
        result = [{"phone": n.get("phone", ""), "friendly_name": n.get("friendly_name", ""), "sid": ""} for n in numbers] if numbers else []
        return {"count": len(result), "numbers": result}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)

def _handle_get_subscription_info(args, ctx):
    from db import get_db_connection, return_db_connection
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT subscription_tier, stripe_customer_id, stripe_subscription_id FROM subscribers WHERE location_id = %s", (ctx["location_id"],))
        row = cur.fetchone()
        cur.close()
        if not row: return {"error": "Account not found"}
        tier_names = {"individual": "Power Dialer ($149.99/mo)", "pro_dialer": "Pro Dialer ($224.99/mo)", "predictive_dialer": "Predictive Dialer ($349.98/mo)", "sms_bot": "SMS Bot ($99.98/mo)"}
        return {"plan": tier_names.get(row[0], row[0] or "none"), "tier": row[0] or "none", "has_billing": bool(row[1])}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)

def _handle_get_ai_minutes_balance(args, ctx):
    from db import get_db_connection, return_db_connection
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT balance FROM ai_minute_balances WHERE location_id = %s", (ctx["location_id"],))
        row = cur.fetchone()
        cur.close()
        balance = row[0] if row else 0
        level = "ok" if balance > 100 else "low" if balance > 20 else "critical" if balance > 0 else "empty"
        return {"balance_minutes": balance, "level": level}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)

def _handle_get_inbox_conversations(args, ctx):
    from db import get_db_connection, return_db_connection
    limit = min(args.get("limit", 10), 30)
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT ON (cm.contact_id) cm.contact_id, cm.message, cm.direction, cm.created_at,
                   cc.first_name, cc.last_name
            FROM contact_messages cm
            LEFT JOIN contact_cache cc ON cc.contact_id = cm.contact_id AND cc.location_id = %s
            WHERE cm.location_id = %s
            ORDER BY cm.contact_id, cm.created_at DESC
        """, (ctx["location_id"], ctx["location_id"]))
        all_rows = cur.fetchall()
        cur.close()
        # Sort by most recent and limit
        all_rows.sort(key=lambda r: r[3] or "", reverse=True)
        convos = []
        for r in all_rows[:limit]:
            convos.append({"contact_id": r[0], "last_message": (r[1] or "")[:100], "direction": r[2] or "", "time": str(r[3])[:16] if r[3] else "", "name": f"{r[4] or ''} {r[5] or ''}".strip() or "Unknown"})
        return {"count": len(convos), "conversations": convos}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)


# ══════════════════════════════════════════════════════════════════════════════
# REMINDERS (Redis-based scheduled tasks)
# ══════════════════════════════════════════════════════════════════════════════

def _handle_set_reminder(args, ctx):
    message = args.get("message", "")
    delay = args.get("delay_minutes", 60)
    contact_id = args.get("contact_id", "")
    if not message: return {"error": "Reminder message required"}
    try:
        from extensions import ensure_redis
        r = ensure_redis()
        if not r: return {"error": "Reminder service unavailable"}
        import uuid
        from datetime import datetime, timezone, timedelta
        remind_at = datetime.now(timezone.utc) + timedelta(minutes=delay)
        reminder = json.dumps({
            "id": str(uuid.uuid4())[:8],
            "message": message,
            "contact_id": contact_id,
            "remind_at": remind_at.isoformat(),
            "email": ctx["email"],
            "location_id": ctx["location_id"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        key = f"reminder:{ctx['email']}:{remind_at.isoformat()}"
        r.setex(key, int(delay * 60) + 60, reminder)
        # Also add to a sorted set for listing
        r.zadd(f"reminders:{ctx['email']}", {reminder: remind_at.timestamp()})
        hours = delay // 60
        mins = delay % 60
        time_label = f"{hours}h {mins}m" if hours > 0 else f"{mins} minutes"
        return {"set": True, "message": message, "in": time_label, "remind_at": remind_at.strftime("%I:%M %p")}
    except Exception as e: return {"error": str(e)[:200]}

def _handle_list_reminders(args, ctx):
    try:
        from extensions import ensure_redis
        r = ensure_redis()
        if not r: return {"reminders": [], "message": "Reminder service unavailable"}
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).timestamp()
        raw = r.zrangebyscore(f"reminders:{ctx['email']}", now, "+inf", withscores=True)
        reminders = []
        for item, score in raw:
            try:
                data = json.loads(item)
                reminders.append({"message": data.get("message", ""), "remind_at": data.get("remind_at", ""), "contact_id": data.get("contact_id", "")})
            except Exception:
                continue
        return {"count": len(reminders), "reminders": reminders} if reminders else {"count": 0, "message": "No pending reminders."}
    except Exception as e: return {"error": str(e)[:200]}


# ══════════════════════════════════════════════════════════════════════════════
# TEAM MANAGEMENT HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

def _handle_list_team_members(args, ctx):
    from db import get_db_connection, return_db_connection
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT full_name, email, role, onboarding_status, is_active FROM location_users WHERE location_id = %s ORDER BY created_at", (ctx["location_id"],))
        members = [{"name": r[0] or "", "email": r[1], "role": r[2], "status": r[3], "active": r[4]} for r in cur.fetchall()]
        cur.close()
        return {"count": len(members), "members": members} if members else {"count": 0, "message": "No team members yet."}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)

def _handle_invite_team_member(args, ctx):
    email = args.get("email", "").strip().lower()
    name = args.get("full_name", "").strip()
    role = args.get("role", "agent")
    if not email: return {"error": "Email required"}
    # Use internal API
    try:
        from flask import current_app
        with current_app.test_request_context(json={"email": email, "full_name": name, "role": role}):
            from flask_login import login_user
            # Can't easily call another blueprint route, so return navigation
            pass
    except Exception:
        pass
    return {"action": "navigate", "tab_id": "team", "message": f"I've opened the Team tab. Enter {email} in the invite form to send the invitation."}


# ══════════════════════════════════════════════════════════════════════════════
# ACTIVITY LOGS
# ══════════════════════════════════════════════════════════════════════════════

def _handle_get_activity_logs(args, ctx):
    from db import get_db_connection, return_db_connection
    limit = min(args.get("limit", 15), 50)
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT event_type, message, created_at FROM webhook_logs WHERE location_id = %s ORDER BY created_at DESC LIMIT %s", (ctx["location_id"], limit))
        logs = [{"type": r[0] or "", "message": (r[1] or "")[:150], "time": str(r[2])[:16] if r[2] else ""} for r in cur.fetchall()]
        cur.close()
        return {"count": len(logs), "logs": logs} if logs else {"count": 0, "message": "No recent activity."}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)


# ══════════════════════════════════════════════════════════════════════════════
# AGENCY OWNER HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

def _handle_get_agency_kpis(args, ctx):
    from db import get_db_connection, return_db_connection
    period = args.get("period", "today")
    date_filters = {"today": "AND ch.created_at >= CURRENT_DATE", "week": "AND ch.created_at >= CURRENT_DATE - INTERVAL '7 days'", "month": "AND ch.created_at >= CURRENT_DATE - INTERVAL '30 days'", "all": ""}
    df = date_filters.get(period, "")
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Get all location_ids under this agency
        cur.execute("SELECT company_id FROM subscribers WHERE location_id = %s", (ctx["location_id"],))
        row = cur.fetchone()
        if not row or not row[0]: return {"error": "Not an agency account"}
        cur.execute("SELECT location_id FROM subscribers WHERE company_id = %s", (row[0],))
        loc_ids = [r[0] for r in cur.fetchall() if r[0]]
        if not loc_ids: return {"error": "No agents found in agency"}
        placeholders = ",".join(["%s"] * len(loc_ids))
        cur.execute(f"""
            SELECT COUNT(*) as total, COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) as connected,
                   COALESCE(SUM(duration) FILTER (WHERE status = 'completed'), 0) as talk_secs,
                   COUNT(DISTINCT location_id) as active_agents
            FROM call_history ch WHERE location_id IN ({placeholders}) {df}
        """, loc_ids)
        r = cur.fetchone()
        cur.close()
        total, connected, secs, agents = r[0] or 0, r[1] or 0, r[2] or 0, r[3] or 0
        rate = round(connected / total * 100, 1) if total > 0 else 0
        return {"period": period, "total_calls": total, "connected": connected, "connect_rate": f"{rate}%", "talk_time": f"{secs//3600}h {(secs%3600)//60}m", "active_agents": agents, "locations": len(loc_ids)}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)

def _handle_get_agent_performance(args, ctx):
    from db import get_db_connection, return_db_connection
    period = args.get("period", "today")
    agent_name = args.get("agent_name", "")
    date_filters = {"today": "AND ch.created_at >= CURRENT_DATE", "week": "AND ch.created_at >= CURRENT_DATE - INTERVAL '7 days'", "month": "AND ch.created_at >= CURRENT_DATE - INTERVAL '30 days'", "all": ""}
    df = date_filters.get(period, "")
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT company_id FROM subscribers WHERE location_id = %s", (ctx["location_id"],))
        row = cur.fetchone()
        if not row or not row[0]: return {"error": "Not an agency account"}
        cur.execute("SELECT location_id, email, full_name FROM subscribers WHERE company_id = %s", (row[0],))
        agents = {r[0]: {"email": r[1], "name": r[2] or r[1]} for r in cur.fetchall() if r[0]}
        results = []
        for lid, info in agents.items():
            if agent_name and agent_name.lower() not in (info.get("name") or "").lower(): continue
            cur.execute(f"SELECT COUNT(*), COUNT(*) FILTER (WHERE status='completed' AND duration>0), COALESCE(SUM(duration) FILTER (WHERE status='completed'),0) FROM call_history ch WHERE location_id=%s {df}", (lid,))
            r = cur.fetchone()
            t, c, s = r[0] or 0, r[1] or 0, r[2] or 0
            results.append({"name": info["name"], "calls": t, "connected": c, "rate": f"{round(c/t*100,1)}%" if t > 0 else "0%", "talk_time": f"{s//3600}h {(s%3600)//60}m" if s >= 3600 else f"{s//60}m"})
        cur.close()
        results.sort(key=lambda x: x["calls"], reverse=True)
        return {"count": len(results), "agents": results}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)

def _handle_get_agency_call_log(args, ctx):
    from db import get_db_connection, return_db_connection
    limit = min(args.get("limit", 20), 50)
    agent_name = args.get("agent_name", "")
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT company_id FROM subscribers WHERE location_id = %s", (ctx["location_id"],))
        row = cur.fetchone()
        if not row or not row[0]: return {"error": "Not an agency account"}
        cur.execute("SELECT location_id, full_name FROM subscribers WHERE company_id = %s", (row[0],))
        agents = {r[0]: r[1] or "Unknown" for r in cur.fetchall() if r[0]}
        loc_ids = list(agents.keys())
        if agent_name:
            loc_ids = [lid for lid, name in agents.items() if agent_name.lower() in name.lower()]
        if not loc_ids: return {"calls": [], "message": "No matching agents"}
        ph = ",".join(["%s"] * len(loc_ids))
        cur.execute(f"SELECT contact_name, phone_number, direction, status, duration, created_at, location_id FROM call_history WHERE location_id IN ({ph}) ORDER BY created_at DESC LIMIT %s", loc_ids + [limit])
        calls = [{"name": r[0] or "Unknown", "phone": r[1] or "", "direction": r[2] or "", "status": r[3] or "", "duration": f"{(r[4] or 0)//60}m", "time": str(r[5])[:16] if r[5] else "", "agent": agents.get(r[6], "Unknown")} for r in cur.fetchall()]
        cur.close()
        return {"count": len(calls), "calls": calls}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)

def _handle_get_agency_leaderboard(args, ctx):
    result = _handle_get_agent_performance({"period": args.get("period", "week")}, ctx)
    if "error" in result: return result
    metric = args.get("metric", "connect_rate")
    agents = result.get("agents", [])
    if metric == "calls": agents.sort(key=lambda x: x["calls"], reverse=True)
    elif metric == "duration": agents.sort(key=lambda x: x.get("talk_time", ""), reverse=True)
    elif metric == "connect_rate": agents.sort(key=lambda x: float(x.get("rate", "0").replace("%", "")), reverse=True)
    return {"leaderboard": agents[:10], "metric": metric, "period": args.get("period", "week")}

def _handle_invite_agency_agent(args, ctx):
    email = args.get("email", "").strip()
    name = args.get("full_name", "").strip()
    if not email: return {"error": "Email required"}
    return {"action": "navigate", "tab_id": "team", "message": f"I've opened the Team tab. Enter {email} ({name}) in the invite form."}

def _handle_list_agency_members(args, ctx):
    from db import get_db_connection, return_db_connection
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT company_id FROM subscribers WHERE location_id = %s", (ctx["location_id"],))
        row = cur.fetchone()
        if not row or not row[0]: return {"error": "Not an agency account"}
        cur.execute("SELECT email, full_name, subscription_tier, location_id FROM subscribers WHERE company_id = %s ORDER BY created_at", (row[0],))
        members = [{"email": r[0], "name": r[1] or r[0], "plan": r[2] or "none", "location_id": r[3] or ""} for r in cur.fetchall()]
        cur.close()
        return {"count": len(members), "members": members}
    except Exception as e: return {"error": str(e)[:200]}
    finally:
        if conn: return_db_connection(conn)


# ── Tool handler registry ────────────────────────────────────────────────────

_TOOL_HANDLERS = {
    # Additional queries (4)
    "get_pipeline_summary": _handle_get_pipeline_summary,
    "search_recordings": _handle_search_recordings,
    "send_bulk_sms": _handle_send_bulk_sms,
    "get_daily_summary": _handle_get_daily_summary,
    # Core (14 original)
    "search_contact": _handle_search_contact,
    "send_sms": _handle_send_sms,
    "check_calendar": _handle_check_calendar,
    "book_appointment": _handle_book_appointment,
    "get_call_stats": _handle_get_call_stats,
    "navigate_dashboard": _handle_navigate_dashboard,
    "get_contact_intelligence": _handle_get_contact_intelligence,
    "make_call": _handle_make_call,
    "queue_dial_session": _handle_queue_dial_session,
    "query_call_history": _handle_query_call_history,
    "get_hot_leads": _handle_get_hot_leads,
    "get_upcoming_appointments": _handle_get_upcoming_appointments,
    "get_recent_messages": _handle_get_recent_messages,
    "get_stale_leads": _handle_get_stale_leads,
    # Contact management (8)
    "edit_contact": _handle_edit_contact,
    "add_contact_tag": _handle_add_contact_tag,
    "remove_contact_tag": _handle_remove_contact_tag,
    "add_contact_note": _handle_add_contact_note,
    "create_contact": _handle_create_contact,
    "move_contact_pipeline": _handle_move_contact_pipeline,
    "get_contact_history": _handle_get_contact_history,
    "mark_do_not_contact": _handle_mark_do_not_contact,
    # Workflows (5)
    "list_workflows": _handle_list_workflows,
    "assign_contact_to_workflow": _handle_assign_contact_to_workflow,
    "assign_bulk_to_workflow": _handle_assign_bulk_to_workflow,
    "toggle_workflow": _handle_toggle_workflow,
    "create_workflow_ai": _handle_create_workflow_ai,
    # Config & settings (7)
    "get_bot_config": _handle_get_bot_config,
    "update_bot_config": _handle_update_bot_config,
    "set_carriers": _handle_set_carriers,
    "list_carriers": _handle_list_carriers,
    "list_phone_numbers": _handle_list_phone_numbers,
    "get_subscription_info": _handle_get_subscription_info,
    "get_ai_minutes_balance": _handle_get_ai_minutes_balance,
    # Inbox & conversations (1)
    "get_inbox_conversations": _handle_get_inbox_conversations,
    # Reminders (2)
    "set_reminder": _handle_set_reminder,
    "list_reminders": _handle_list_reminders,
    # Team (2)
    "list_team_members": _handle_list_team_members,
    "invite_team_member": _handle_invite_team_member,
    # Activity (1)
    "get_activity_logs": _handle_get_activity_logs,
    # Agency owner (6)
    "get_agency_kpis": _handle_get_agency_kpis,
    "get_agent_performance": _handle_get_agent_performance,
    "get_agency_call_log": _handle_get_agency_call_log,
    "get_agency_leaderboard": _handle_get_agency_leaderboard,
    "invite_agency_agent": _handle_invite_agency_agent,
    "list_agency_members": _handle_list_agency_members,
}
