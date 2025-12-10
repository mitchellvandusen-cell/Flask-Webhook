# NEPQ Webhook API

## Overview

This is a Flask-based webhook API service that generates AI-powered sales responses using NEPQ (Neuro-Emotional Persuasion Questioning) methodology by Jeremy Miner. The service receives inbound SMS/message data via webhooks, processes them through xAI's Grok model, and returns personalized sales responses optimized for SMS communication.

The primary use case is life insurance lead re-engagement, where the AI assistant helps book phone appointments by asking strategic questions rather than using pushy sales tactics.

## User Preferences

- Preferred communication style: Simple, everyday language
- No database required - simple stateless API
- No em dashes (--) in responses
- Root URL accepts POST directly for webhook

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

That's it! The API will generate an NEPQ response and automatically send it back to the contact via SMS.

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
