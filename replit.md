# NEPQ Webhook API

## Overview

This is a Flask-based webhook API service that generates AI-powered sales responses using comprehensive NEPQ (Neuro-Emotional Persuasion Questioning) methodology by Jeremy Miner. The service receives inbound SMS/message data via webhooks, processes them through xAI's Grok model, and returns personalized sales responses optimized for SMS communication.

The primary use case is life insurance lead re-engagement, where the AI assistant helps book phone appointments by asking strategic questions rather than using pushy sales tactics.

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

### Backend Framework
- **Flask** serves as the web framework
- Single-file architecture in `main.py` for simplicity

### AI Integration
- Uses **xAI's Grok API** via direct HTTP requests
- Base URL: `https://api.x.ai/v1`
- Model: `grok-4-1-fast-reasoning`
- Comprehensive NEPQ system prompt with full methodology

### NEPQ Methodology Included
The system prompt contains complete NEPQ framework:

**Question Framework:**
- Problem Awareness Questions (discover pain points)
- Consequence Questions (deepen urgency)
- Solution Awareness Questions (guide to next step)
- Commitment Questions (book appointments)

**Objection Handling:**
- "Can't afford it" / pricing objections
- "Already have insurance through work"
- "Need to think about it" / spouse objections
- "Don't trust insurance companies"
- "Too young" / "Too old" objections
- "Send me information" / email requests
- "Busy" / timing objections
- "Not interested" rejections
- Pricing/cost questions

**Special Features:**
- Handles weird/off-topic questions by redirecting to booking
- Auto-generates random confirmation codes for appointments
- Never answers questions it shouldn't - always redirects to booking call

### API Design
- Main webhook endpoint: `POST /grok`
- Accepts JSON with `first_name` and `message` fields
- Returns JSON with `reply` field containing AI response
- Health check: `GET /health`
- API docs: `GET /`

## Endpoints

### POST /grok
Process lead message and generate NEPQ response.

**Request:**
```json
{
  "first_name": "John",
  "message": "I saw your ad about life insurance"
}
```

**Response:**
```json
{
  "reply": "What originally got you looking at life insurance, John?"
}
```

### GET /health
Health check endpoint.

### GET /
API documentation.

## Environment Variables Required
- `SESSION_SECRET`: Flask session encryption key
- `XAI_API_KEY`: xAI/Grok API authentication

## Key Files
- `main.py` - Complete Flask application with NEPQ system prompt
