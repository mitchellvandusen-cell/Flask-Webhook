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
- Uses `grok-4-1-fast-reasoning` model (10x cheaper than grok-2-1212: $0.20/$0.50 vs $2/$10 per million tokens).

### Four-Layer Conversation Architecture
0.  **Layer 0: Deterministic Trigger Map (`force_response()`)**: Pattern-matching for common scenarios (term policy, GI, buying signals, price questions, health conditions) that return instant responses without LLM calls. Saves ~40% API costs on common patterns. Only fetches calendar slots when needed (lazy loading).
1.  **Layer 1: Base Model (Grok)**: Generates AI responses with self-reflection scores for messages that don't match triggers.
2.  **Layer 2: Conversation State Machine (`conversation_engine.py`)**:
    *   Manages `ConversationState` (stage, exchange count, facts extracted, dismissive counts).
    *   Performs deterministic stage detection (INITIAL_OUTREACH, DISCOVERY, CONSEQUENCE, CLOSING).
    *   **PolicyEngine**: Validates responses for format, self-reflection scores, stage-specific rules, and repeat questions.
3.  **Layer 3: Playbook Library (`playbook.py`)**: Provides template responses for common scenarios and fallbacks if LLM validation fails.

### Dynamic Calendar Integration
- `get_available_slots()`: Queries GHL calendar API for real appointment availability.
- `format_slot_options()`: Formats slots with context-aware time labels (tonight, tomorrow morning, etc.).
- Lazy loading: Calendar API only called when booking-related triggers (BUYING_SIGNAL, PRICE) match.
- Fallback: Uses "6:30 tonight or 10:15 tomorrow morning" if calendar unavailable.

### Already Covered Objection Handler (State Machine)
-   **Deterministic pathway**: Handles "I already have coverage" objections WITHOUT LLM calls.
-   **3-Step Flow to Appointment**:
    1.  Lead says "already have/covered/set" → "Who'd you go with?"
    2.  Lead names carrier → "Oh did someone help you get set up with them or did you find them yourself? They usually help people with higher risk, do you have serious health issues?"
    3.  Lead says no/healthy → Context-aware doubt seeding:
        - If someone helped: "Weird they put you with them. I mean they're a good company..."
        - If found themselves: "I mean they're a good company, like I said they just take higher risk people so it's usually more expensive for healthier people like yourself. I have some time tonight or tomorrow, I can do a quick review and just make sure you're not overpaying. Which works best for you?"
    4.  Lead picks time → "Perfect, got you down for [time]. Quick question so I can have the best options ready, are you taking any medications currently?"
    5.  Lead answers meds → Confirm appointment, calendar invite coming
-   **Shortcuts**:
    - Employer-based coverage → Instant living benefits gap pitch + appointment offer
    - Carrier mentioned in initial message → Skip to step 2
    - Lead says YES they're sick → Empathetic pivot to appointment
-   **Goal**: Subtle doubt-seeding to justify a quick review appointment. If we can't beat their current situation, we tell them honestly.
-   **State fields**: `objection_path`, `already_handled`, `waiting_for_health`, `carrier_gap_found`, `waiting_for_medications`, `appointment_time`, `medications`

### Contact Qualification State (Persistent Memory)
-   **`contact_qualification` table**: Stores persistent qualification data per contact_id across all messages and conversations.
-   **Auto-extraction**: Automatically extracts qualification data from each message (policy type, carrier, family, health, blockers, etc.).
-   **Fields tracked**:
    *   Coverage: `has_policy`, `is_term`, `is_whole_life`, `is_iul`, `is_guaranteed_issue`, `term_length`, `face_amount`
    *   Source: `is_personal_policy`, `is_employer_based`, `carrier`, `has_living_benefits`
    *   Family: `has_spouse`, `num_kids`, `has_dependents`
    *   Health: `health_conditions[]`, `tobacco_user`, `age`, `retiring_soon`
    *   Motivation: `motivating_goal`, `blockers[]`
    *   Tracking: `total_exchanges`, `topics_asked[]`, `conversation_stage`
    *   Flow State: `waiting_for_other_policies`, `waiting_for_goal`, `has_other_policies`
-   **Prompt injection**: Known facts are injected into prompts to prevent repeat questions.
-   **Topic-based repeat prevention**: Blocks asking about topics already covered (living benefits, portability, amount, term length, company).

### Semantic Duplicate Prevention (Relevancy Tracker)
-   **Theme-based blocking**: Questions are categorized into themes (retirement_portability, policy_type, living_benefits, coverage_goal, other_policies, motivation).
-   **75% similarity check**: If a new question shares a theme with any of the last 5 agent messages, it's blocked.
-   **Logical inference blocking**: When `is_personal_policy=true` or `is_employer_based=false`, all retirement/portability questions are automatically blocked.
-   **State-aware blocking**: If `has_living_benefits` is known, won't ask about it. If `has_other_policies` is known, won't ask again.
-   **Fallback behavior**: When a duplicate is blocked, uses a progression question (appointment offer) instead of repeating.

### Private Policy Flow (Updated)
When lead says "not an employer policy" / "private" / "personal" / "not through work":
1. Sets `is_personal_policy=true`, `is_employer_based=false`
2. Adds `employer_portability` and `job_coverage` to `topics_asked` (blocks future questions)
3. Asks "Any other policies through work or otherwise?" (`waiting_for_other_policies=true`)
4. After answer, asks "What made you want to look at coverage originally, was it to add more, cover a mortgage, or something else?" (`waiting_for_goal=true`)
5. Captures goal as `motivating_goal` (add_coverage, cover_mortgage, final_expense, family_protection, etc.)

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
-   **Insurance Company Recognition** (`insurance_companies.py`): Validates 100+ major US life insurance carriers to prevent double-messaging (e.g., won't ask "who'd you go with" if lead already named the company). Smart context detection: only Colonial Penn/Globe Life assumed GI; other carriers require corroborating phrases.

## External Dependencies

-   **xAI's Grok API**: For AI model inference (`https://api.x.ai/v1`, model: `grok-4-1-fast-reasoning`).
-   **GoHighLevel (GHL)**: For CRM and marketing automation integration, including sending SMS, managing contacts, opportunities, and calendars.
-   **PostgreSQL**: Database for storing response patterns, contact history, and outcome tracking.