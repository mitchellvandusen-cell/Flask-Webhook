# NEPQ Webhook API

## Overview

This is a Flask-based webhook API service that generates AI-powered sales responses using a blended sales methodology for life insurance lead re-engagement. The service receives inbound SMS/message data via webhooks, processes them through xAI's Grok model, and returns personalized sales responses optimized for SMS communication.

### Sales Framework Blend
The AI uses five complementary frameworks:
1. **NEPQ (Primary)**: Neuro-Emotional Persuasion Questioning by Jeremy Miner - questions create curiosity and uncover problems
2. **Straight Line Persuasion**: Jordan Belfort - every message moves toward the goal, redirect elegantly when derailed
3. **Psychology of Selling**: Brian Tracy - persistence wins, rejection is redirection, stay calm and curious
4. **Never Split the Difference**: Chris Voss FBI negotiation - tactical empathy, calibrated questions ("How am I supposed to do that?"), labeling emotions ("It sounds like..."), mirroring, and getting to "That's right"
5. **Gap Selling**: Keenan - understand Current State (where they are) vs Future State (where they want to be), the GAP is the value you provide; be an expert, not a friend

### Target Leads
- Cold leads 30 days to 6+ months old
- Previously looked at life insurance but never purchased
- Most haven't thought about insurance in months
- Guard is UP, will try to end conversations early

### Coverage Gap Analysis
The agent has deep knowledge of life insurance scenarios and can identify:

**Age-Based Awareness:**
- 20-30: Lock in rates young, term vs IUL/permanent considerations
- 30-45: Peak responsibility years, 10-15x income replacement target
- 45-55: Health issues appearing, term policies expiring
- 55-65: Retirement coverage crisis, employer coverage ending
- 65+: Final expense, legacy planning

**Coverage Traps:**
- Employer coverage: Doesn't follow you, group rates disappear at retirement
- Bundled policies (State Farm): Often minimal coverage for bundle discount
- Term expiration: 30-year term at 25 = renewal at 55 at 5-10x rates
- Coverage math: $50k policy vs $300k mortgage = family must sell house

**Smart Probing:**
- "Does it follow you if you switch jobs or retire?"
- "What's your plan when the term expires?"
- "With a 300k mortgage, how long would 50k last your family?"
- Handles family/kids impact sensitively but honestly

### Lead Profile Memory
Server-side extraction of conversation data prevents duplicate questions:
- Tracks: family, coverage, motivating goal, blockers, health, age
- Only extracts from LEAD messages (not agent questions)
- Formats as explicit "DO NOT ASK ABOUT" list for the AI
- Uses stored info for consequence-based closes when client resists

## User Preferences

- Preferred communication style: Simple, everyday language
- No database required - simple stateless API
- No em dashes (--) in responses
- Root URL accepts POST directly for webhook
- Don't overuse first name - only every 3-4 messages like normal texting
- Agent interprets what customer REALLY means (e.g., "got coverage through work" = "stop texting me")
- Conversation-first approach - find problems before suggesting appointments

## System Architecture

### Backend Framework
- **Flask** serves as the web framework
- Single-file architecture in `main.py` for simplicity

### AI Integration
- Uses **xAI's Grok API** via OpenAI-compatible client
- Base URL: `https://api.x.ai/v1`
- Model: `grok-2-1212`
- Comprehensive NEPQ system prompt with full methodology

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/ghl` | POST | **Unified GHL endpoint** - handles all actions (respond, appointment, stage, contact, search) |
| `/` | POST | Main webhook - process message and return NEPQ response |
| `/grok` | POST | Alias for main webhook |
| `/webhook` | POST | Alias for main webhook |
| `/ghl-webhook` | POST | Legacy - redirects to /ghl with action=respond |
| `/ghl-appointment` | POST | Legacy - redirects to /ghl with action=appointment |
| `/ghl-stage` | POST | Legacy - redirects to /ghl with action=stage |
| `/outreach` | GET/POST | Returns "Up and running" (GET) or "OK" (POST) |
| `/health` | GET | Health check endpoint |

## Multi-Tenant Support

The `/ghl` endpoint supports multiple users (you and your friends) by accepting GHL credentials in the request body:

```json
{
  "action": "respond",
  "ghl_api_key": "your-friends-api-key",
  "ghl_location_id": "your-friends-location-id",
  "contact_id": "abc123",
  "first_name": "John",
  "message": "I saw your ad"
}
```

If `ghl_api_key` and `ghl_location_id` are not provided, the API falls back to environment variables (your default setup).

## GHL Webhook Setup

When setting up the webhook in GoHighLevel:

**URL:** `https://InsuranceGrokBot.replit.app/`

**Custom Data fields:**
| Key | Value |
|-----|-------|
| contact_id | {{contact.id}} |
| first_name | {{contact.first_name}} |
| message | {{message.body}} |
| agent_name | Mitchell |

Replace "Mitchell" with your name (or Devon, etc). The AI will identify as that person.

For multi-tenant (friends using their own GHL accounts), also add:
| Key | Value |
|-----|-------|
| ghl_api_key | (their private integration token) |
| ghl_location_id | (their location ID) |
| agent_name | Devon |

**Optional intent field** - controls the AI's objective:
| Key | Value |
|-----|-------|
| intent | book_appointment |

That's it! The API will generate an NEPQ response and automatically send it back to the contact via SMS.

## Intent Recognition

The AI recognizes the "intent" custom field to adjust its approach. Supported intents:

| Intent | Description |
|--------|-------------|
| `book_appointment` | Push for specific time slots, close the deal |
| `qualify` | Ask Stage 1 NEPQ questions to uncover pain points |
| `reengage` | Soft, curious opener for cold leads who haven't responded |
| `follow_up` | Continue from previous conversation, check for questions |
| `nurture` | Keep relationship warm, build rapport without pushing |
| `objection_handling` | Use curiosity to understand and address concerns |
| `initial_outreach` | First message, introduce and ask what got them looking |
| `general` | Default, follow standard NEPQ framework |

Aliases work too: "book", "booking", "schedule" all map to `book_appointment`.

## Guaranteed Issue Qualification Workflow

The AI automatically detects when leads have "guaranteed issue" type products and probes their health to find better options:

**Trigger Detection:**
- "no health questions" / "guaranteed issue" / "guaranteed acceptance"
- Provider names: Colonial Penn, Globe Life, AARP
- "I have health issues" / "I can't qualify anywhere"

**Health Probing (sensitive, conversational):**
| Condition | Follow-up Questions |
|-----------|-------------------|
| Diabetes | Pills or insulin? A1C level? How long? |
| Heart/Cardiac | Full heart attack or stent? How long ago? Stable now? |
| COPD | Mild or severe? Oxygen use? Still smoking? |
| Cancer | Type? How long ago? In remission? |
| Stroke | How long ago? Any lasting effects? |

**Carrier Mapping:**
The AI cross-references health conditions against a detailed underwriting guide with 40+ carriers including:
- **Diabetes**: A1C thresholds (under 8%, 8-8.6%, 8.7-9.9%), insulin usage, time on insulin, age at diagnosis
- **Heart Conditions**: Time since heart attack/stent (6mo, 1yr, 2yr, 3yr thresholds), CHF (very limited options)
- **COPD**: Oxygen use, tobacco status, time since diagnosis
- **Stroke**: Time since event, recovery status, presence of diabetes
- **Cancer**: Remission period, type, recurrence history
- **Mental Health**: Hospitalization history, medication status

The AI gives verdicts based on this data:
- **Tough cases** (A1C 9+, uncontrolled 10+ years): Honest about limited options
- **Hopeful cases** (A1C under 8.5, controlled conditions): Offers appointment times
- **Borderline cases**: Worth exploring, offers to dig into it

**Need Statement:**
After qualification, the AI creates a need-based appointment reason: "Based on what you told me, you might not need guaranteed issue. Some carriers accept [condition] with no waiting period. Want me to look into it?"

**Closing Rule:**
When lead shows interest ("yeah that sounds good", "I'd like to look into that"), the AI immediately offers appointment times.

## Unified /ghl Endpoint Actions

### action: "respond" (default)
Generate NEPQ response and send SMS
```json
{
  "action": "respond",
  "contact_id": "{{contact.id}}",
  "first_name": "{{contact.first_name}}",
  "message": "{{message.body}}"
}
```

### action: "appointment"
Create calendar appointment
```json
{
  "action": "appointment",
  "contact_id": "abc123",
  "calendar_id": "cal123",
  "start_time": "2024-01-15T18:30:00Z",
  "duration_minutes": 30,
  "title": "Life Insurance Consultation"
}
```

### action: "stage"
Update or create opportunity
```json
{
  "action": "stage",
  "opportunity_id": "opp123",
  "stage_id": "stage456"
}
```
Or create new:
```json
{
  "action": "stage",
  "contact_id": "abc123",
  "pipeline_id": "pipe789",
  "stage_id": "stage456",
  "name": "Life Insurance Lead"
}
```

### action: "contact"
Get contact info
```json
{
  "action": "contact",
  "contact_id": "abc123"
}
```

### action: "search"
Search contacts by phone
```json
{
  "action": "search",
  "phone": "+15551234567"
}
```

## Response Format

```json
{
  "success": true,
  "reply": "What originally got you looking at life insurance, John?",
  "contact_id": "abc123",
  "sms_sent": true,
  "confirmation_code": "7K9X",
  "appointment_created": false,
  "booking_attempted": false,
  "booking_error": null,
  "time_detected": null
}
```

### Auto-Booking Response (when time is detected)
```json
{
  "success": true,
  "reply": "You're all set for Thursday, December 11 at 02:00 PM...",
  "appointment_created": true,
  "appointment_time": "Thursday, December 11 at 02:00 PM",
  "booking_attempted": true,
  "booking_error": null,
  "time_detected": "Thursday, December 11 at 02:00 PM"
}
```

### Booking Failure Response (HTTP 422)
```json
{
  "success": false,
  "booking_attempted": true,
  "booking_error": "Calendar not configured",
  "time_detected": "Thursday, December 11 at 02:00 PM"
}
```

## Environment Variables Required
- `SESSION_SECRET`: Flask session encryption key
- `XAI_API_KEY`: xAI/Grok API authentication
- `GHL_API_KEY`: GoHighLevel Private Integration Token (optional if passed in body)
- `GHL_LOCATION_ID`: GoHighLevel Location ID (optional if passed in body)
- `GHL_CALENDAR_ID`: GoHighLevel Calendar ID for auto-booking appointments

## Key Features
- NEPQ methodology for non-pushy sales
- Single unified `/ghl` endpoint for all GHL operations
- Multi-tenant support via request body credentials (including calendar_id)
- Automatic confirmation code generation for appointments
- Em dash filtering (replaced with commas)
- Short SMS-friendly responses (15-40 words)
- **Auto-booking**: Detects natural language times (e.g., "tuesday at 10am", "tomorrow afternoon") and automatically creates calendar appointments
- **Dynamic assignedUserId**: Automatically fetches the team member from calendar metadata
- Timezone-aware scheduling (defaults to Central Time)
- Proper error handling with HTTP 422 for booking failures

## Test Contact
- **Mitchell VanDusen**: Contact ID `ETrze7esz1r1kAG9rgfN`, Phone: 605-900-6562

## Key Files
- `main.py` - Complete Flask application with NEPQ system prompt
