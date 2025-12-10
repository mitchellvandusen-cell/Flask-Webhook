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
| `/` | POST | Main webhook - process message and return NEPQ response |
| `/grok` | POST | Alias for main webhook |
| `/webhook` | POST | Alias for main webhook |
| `/ghl-webhook` | POST | GoHighLevel webhook - processes message and sends SMS response via GHL API |
| `/ghl-appointment` | POST | Create appointment in GoHighLevel calendar |
| `/ghl-stage` | POST | Update opportunity stage or create new opportunity in GHL |
| `/outreach` | GET/POST | Returns "Up and running" (GET) or "OK" (POST) |
| `/health` | GET | Health check endpoint |

## Request Formats

### Basic Webhook (/, /grok, /webhook)
```json
{
  "first_name": "John",
  "message": "I saw your ad about life insurance"
}
```

### GHL Webhook (/ghl-webhook)
```json
{
  "contact_id": "abc123",
  "first_name": "John",
  "message": "I saw your ad about life insurance"
}
```

### GHL Appointment (/ghl-appointment)
```json
{
  "contact_id": "abc123",
  "calendar_id": "cal123",
  "start_time": "2024-01-15T18:30:00Z",
  "duration_minutes": 30,
  "title": "Life Insurance Consultation"
}
```

### GHL Stage (/ghl-stage)
Update existing opportunity:
```json
{
  "opportunity_id": "opp123",
  "stage_id": "stage456"
}
```

Create new opportunity:
```json
{
  "contact_id": "abc123",
  "pipeline_id": "pipe789",
  "stage_id": "stage456",
  "name": "Life Insurance Lead"
}
```

## Response Format

```json
{
  "reply": "What originally got you looking at life insurance, John?"
}
```

## Environment Variables Required
- `SESSION_SECRET`: Flask session encryption key
- `XAI_API_KEY`: xAI/Grok API authentication
- `GHL_API_KEY`: GoHighLevel Private Integration Token
- `GHL_LOCATION_ID`: GoHighLevel Location ID

## Key Features
- NEPQ methodology for non-pushy sales
- Automatic confirmation code generation for appointments
- Em dash filtering (replaced with commas)
- Short SMS-friendly responses (15-40 words)

## Key Files
- `main.py` - Complete Flask application with NEPQ system prompt
