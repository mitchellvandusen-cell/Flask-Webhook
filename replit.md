# NEPQ Webhook API

## Overview

This is a Flask-based webhook API service that generates AI-powered sales responses using NEPQ (Neuro-Emotional Persuasion Questioning) methodology. The service receives inbound SMS/message data via webhooks, processes them through xAI's Grok model, and returns personalized sales responses optimized for SMS communication.

The primary use case is life insurance lead re-engagement, where the AI assistant helps book phone appointments by asking strategic questions rather than using pushy sales tactics.

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

### Backend Framework
- **Flask** serves as the web framework
- **Flask-SQLAlchemy** with a custom `DeclarativeBase` handles ORM operations
- **ProxyFix middleware** is applied for proper proxy header handling (x_proto, x_host)

### Database Design
PostgreSQL database with four main models:
- **WebhookLog**: Stores all webhook requests/responses for analytics (includes processing time, status, IP tracking)
- **ConversationHistory**: Maintains message context per contact for continuity across conversations
- **NEPQPersona**: Stores customizable AI personas with different system prompts
- **RateLimitEntry**: Tracks per-IP request counts within time windows

### AI Integration
- Uses **xAI's Grok API** via OpenAI-compatible client library
- Base URL: `https://api.x.ai/v1`
- Model: `grok-4-1-fast-reasoning`
- Lazy client initialization pattern to defer API key validation
- Configurable system prompts through personas, with a default NEPQ prompt built-in

### API Design
- Main webhook endpoint: `POST /grok`
- Accepts JSON with `first_name` and `message` fields
- Returns JSON with `status`, `reply`, and metadata
- Rate limiting: 60 requests per IP per 60-second window

### Admin Interface
- Simple Bootstrap-based dashboard for monitoring
- Password-protected access via session authentication
- Features: webhook log viewing, persona management, analytics stats
- Templates use Jinja2 inheritance with a base layout

### Security Measures
- Rate limiting decorator pattern on endpoints
- HMAC signature verification capability (imported but implementation truncated in routes)
- Session-based admin authentication
- Environment variables for secrets (SESSION_SECRET, XAI_API_KEY, DATABASE_URL)

## External Dependencies

### APIs & Services
- **xAI Grok API**: AI text generation (requires XAI_API_KEY environment variable)
- **PostgreSQL**: Primary database (requires DATABASE_URL environment variable)

### Key Python Packages
- `flask`: Web framework
- `flask-sqlalchemy`: Database ORM
- `openai`: Client library for xAI API (OpenAI-compatible)
- `werkzeug`: WSGI utilities and proxy handling

### Environment Variables Required
- `SESSION_SECRET`: Flask session encryption key
- `XAI_API_KEY`: xAI/Grok API authentication
- `DATABASE_URL`: PostgreSQL connection string