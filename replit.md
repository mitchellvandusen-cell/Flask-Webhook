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
| `/outreach` | GET/POST | Returns "Up and running" (GET) or "OK" (POST) |
| `/health` | GET | Health check endpoint |

## Request Format

```json
{
  "first_name": "John",
  "message": "I saw your ad about life insurance"
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

## Key Features
- NEPQ methodology for non-pushy sales
- Automatic confirmation code generation for appointments
- Em dash filtering (replaced with commas)
- Short SMS-friendly responses (15-40 words)

## Key Files
- `main.py` - Complete Flask application with NEPQ system prompt
