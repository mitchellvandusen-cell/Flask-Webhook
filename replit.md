# NEPQ Webhook API

## Overview

This Flask-based webhook API generates AI-powered sales responses for life insurance lead re-engagement. It processes inbound SMS/message data, leverages xAI's Grok model with a blended sales methodology (NEPQ, Straight Line Persuasion, Psychology of Selling, Never Split the Difference, Gap Selling), and returns personalized, SMS-optimized responses. The system targets cold leads, identifies coverage gaps, and uses a Socratic approach to foster problem awareness, ultimately aiming to secure appointments for life insurance consultations. It also features a lead profile memory to avoid repetitive questions and an outcome-based learning system to refine response patterns.

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
- **Flask** is used as the web framework, implemented in a single file (`main.py`) for simplicity.

### AI Integration
- Integrates with **xAI's Grok API** via an OpenAI-compatible client.
- Uses `grok-2-1212` model with a comprehensive NEPQ system prompt.

### Three-Layer Conversation Architecture
1.  **Layer 1: Base Model (Grok)**: Generates initial responses including self-reflection scores.
2.  **Layer 2: Conversation State Machine (`conversation_engine.py`)**:
    *   Manages `ConversationState` (stage, exchange count, facts extracted, dismissive counts).
    *   Performs deterministic stage detection (INITIAL_OUTREACH, DISCOVERY, CONSEQUENCE, CLOSING).
    *   **PolicyEngine**: Validates responses for format, self-reflection scores, stage-specific rules, and repeat questions.
3.  **Layer 3: Playbook Library (`playbook.py`)**: Provides template responses for common scenarios and fallbacks if LLM validation fails.

### Outcome-Based Learning System
-   Utilizes PostgreSQL to store successful `response_patterns`, `contact_history`, and `outcome_tracker`.
-   Classifies lead responses into "vibes" (ghosted, dismissive, objection, neutral, information, direction, need) to score agent messages.
-   Maintains "Forward patterns" for engaged leads and "Recovery patterns" for objections/burned contacts.
-   Injects high-scoring patterns into future prompts as "PROVEN RESPONSES".
-   Tracks "burned" contacts and applies a +2.0 bonus to all conversation responses upon appointment booking.

### API Endpoints
-   **`/ghl` (POST)**: Unified endpoint for all GoHighLevel actions (respond, appointment, stage, contact, search), supporting multi-tenant via request body credentials.
-   **`/`, `/grok`, `/webhook` (POST)**: Main webhooks for processing messages.
-   **`/outreach`, `/health`**: Utility endpoints.

### Key Features
-   NEPQ methodology and smart probing for life insurance.
-   Multi-tenant support.
-   Automatic confirmation code generation.
-   SMS-friendly responses (15-40 words).
-   **Auto-booking**: Detects natural language times for appointment scheduling and integrates with GHL calendars, handling timezones and dynamic `assignedUserId`.
-   **Intent Recognition**: AI adjusts its approach based on specified intents (e.g., `book_appointment`, `qualify`, `reengage`).
-   **Guaranteed Issue Qualification Workflow**: Detects "guaranteed issue" inquiries, probes health conditions, cross-references with carrier underwriting guides, and provides tailored appointment justifications.

## External Dependencies

-   **xAI's Grok API**: For AI model inference (`https://api.x.ai/v1`, model: `grok-2-1212`).
-   **GoHighLevel (GHL)**: For CRM and marketing automation integration, including sending SMS, managing contacts, opportunities, and calendars.
-   **PostgreSQL**: Database for storing response patterns, contact history, and outcome tracking.