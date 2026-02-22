# InsuranceGrokBot — Technical Owner's Manual

**Last updated: 2026-02-22**

This document explains how the system actually works, written in plain English based on a full review of the live codebase. Think of this as the car owner's manual: not "here's the philosophy," but "here's where the engine is, why each part exists, and what to do when it makes a weird noise."

---

## Table of Contents

1. [System Overview — The Big Picture](#1-system-overview--the-big-picture)
2. [How a Webhook Gets Processed](#2-how-a-webhook-gets-processed)
3. [The Database Layer (db.py)](#3-the-database-layer-dbpy)
4. [The Memory System (memory.py)](#4-the-memory-system-memorypy)
5. [The AI Pipeline (tasks.py)](#5-the-ai-pipeline-taskspy)
6. [The System Prompt (prompt.py)](#6-the-system-prompt-promptpy)
7. [The LLM Caller (llm_caller.py)](#7-the-llm-caller-llm_callerpy)
8. [Reply Safety (reply_sanitizer.py)](#8-reply-safety-reply_sanitizerpy)
9. [Contact Resolution (contact_validator.py)](#9-contact-resolution-contact_validatorpy)
10. [The Voice Bridge (voice_bridge.py)](#10-the-voice-bridge-voice_bridgepy)
11. [The External API (api_v1.py)](#11-the-external-api-api_v1py)
12. [The Web Server (main.py)](#12-the-web-server-mainpy)
13. [Background Workers (worker.py)](#13-background-workers-workerpy)
14. [CRM Integration (ghl_api.py, crm_adapters/)](#14-crm-integration-ghl_apipy-crm_adapters)
15. [Billing and Subscriptions](#15-billing-and-subscriptions)
16. [Discord Integration](#16-discord-integration)
17. [Security Model](#17-security-model)
18. [Environment Variables Reference](#18-environment-variables-reference)
19. [Troubleshooting Guide](#19-troubleshooting-guide)

---

## 1. System Overview — The Big Picture

InsuranceGrokBot is a multi-tenant SaaS platform. Each "tenant" is an insurance agency that subscribes to the service. Here is what happens at a high level:

1. An insurance lead fills out a form online. That lead gets added to the agency's GoHighLevel (GHL) CRM.
2. GHL fires a webhook to this app.
3. The app looks up that subscriber's account, fetches the lead's conversation history, builds a carefully crafted prompt, calls the xAI Grok API, and sends the reply via Twilio SMS — all without the lead ever knowing Twilio or this app is involved.

The agency sees it as their own branded bot. The lead just sees an SMS from the agent's phone number.

### The Stack in Plain English

- **Flask** — The web framework that answers HTTP requests. Runs inside Gunicorn, which is a production-grade server.
- **Gunicorn** — Runs Flask with 4 threads so multiple requests can be handled simultaneously.
- **PostgreSQL** — The main database. Stores subscriber configs, conversation history, facts about leads, billing, etc.
- **Redis + RQ** — Redis is a fast in-memory database used as a job queue. RQ (Redis Queue) is a library that lets Flask hand off slow work to background workers so the web server can respond immediately.
- **xAI Grok** — The LLM (large language model) that generates SMS replies and voice responses.
- **Twilio** — Sends and receives SMS messages and phone calls. Each agency gets their own Twilio sub-account for white-label isolation.
- **Stripe** — Handles subscription billing.
- **GoHighLevel (GHL/Lead Connector)** — The CRM platform that agencies use to manage their leads. This app integrates via GHL's OAuth API.

---

## 2. How a Webhook Gets Processed

This is the most important flow to understand. Every SMS conversation starts here.

### Step 1 — GHL Fires the Webhook

When a lead sends an SMS or a new lead is created, GHL sends an HTTP POST to `POST /webhook` on this server. The body is JSON containing the lead's contact ID, location ID, message text, first name, etc.

### Step 2 — Signature Verification

`main.py` receives the request. It verifies that the request is actually from GHL by checking an HMAC signature against the `MARKETPLACE_WEBHOOK_SECRET` environment variable. If the signature is invalid, the request is rejected immediately with a 401.

### Step 3 — Payload Normalization

GHL's webhook format has evolved over time, and different event types use different field names. The `normalize_payload_universal()` function in `main.py` converts all the different formats into one clean internal format with consistent snake_case field names like `contact_id`, `location_id`, `message`, `first_name`, etc.

### Step 4 — Job Enqueued to Redis

Rather than processing the webhook synchronously (which would time out under load), the app serializes the normalized payload and enqueues it as a job in Redis. The queue name is either `production` (for real accounts) or `demo` (for demo sessions). The HTTP response is sent back to GHL immediately with a 200 OK so GHL knows the webhook was received.

This is the restaurant analogy used in the code comments: Flask is the front-of-house waiter who takes the order and hands a ticket to the kitchen. The worker is the chef who actually makes the food. The waiter doesn't stand in the kitchen waiting — they go take more orders.

### Step 5 — Worker Picks Up the Job

One of the background RQ workers is watching the `production` queue. It picks up the job and calls `process_webhook_task(payload)` in `tasks.py`.

### Step 6 — Full Processing Pipeline

Inside `process_webhook_task()`, the following happens in order:

1. **Contact validation** — Is the `contact_id` valid? If not, attempt to resolve it via phone number or name lookup.
2. **Subscriber lookup** — Load the agency's configuration from the database.
3. **Token refresh** — Get a fresh GHL OAuth access token for the API calls.
4. **History sync** — If the conversation history database is empty for this contact, pull the full history from GHL's API. If it has 3 or fewer messages, sync recent messages. Otherwise trust the local database.
5. **Message extraction** — Pull the actual text of the lead's message from the payload.
6. **Idempotency check** — Use an atomic `INSERT ... ON CONFLICT DO NOTHING` into the `processed_webhooks` table to make sure this exact webhook ID hasn't been processed before. If it has, skip it.
7. **Message save** — Save the lead's message to the local database.
8. **TCPA check** — If the message contains "stop", "unsubscribe", or "blocked", immediately halt and return without responding. This is a legal compliance requirement.
9. **Message batching** — Wait 3 seconds to let rapid-fire messages from the lead (e.g., three texts in 10 seconds) all arrive and get saved. Then collect all unanswered lead messages and combine them into one. This prevents the bot from responding to each message separately.
10. **Sales Director** — Call `generate_strategic_directive()` which analyzes the conversation stage, recent exchanges, and lead facts to produce tactical guidance for the AI.
11. **Booking detection** — Check if the lead is accepting an appointment time. If yes, call the GHL calendar API to actually book it.
12. **Calendar slots** — If the conversation is in the booking stage but no appointment was made yet, fetch available calendar slots to offer to the lead.
13. **System prompt build** — Assemble the full prompt that guides the LLM.
14. **LLM call** — Send the prompt and conversation history to xAI Grok to generate a reply.
15. **Reply safety check** — Run the reply through `sanitize_reply()` and `is_safe_to_send()` to catch any LLM reasoning that leaked into the response.
16. **Send SMS** — Deliver the reply via Twilio or GHL's SMS API.
17. **Save bot message** — Store the bot's reply in the local database.
18. **Log** — Write an event to `webhook_logs` for the activity dashboard.

---

## 3. The Database Layer (db.py)

**File:** `db.py` (~97KB)

### Why It Exists

All persistent data — subscriber configs, conversation history, facts about leads, billing, audit logs — lives in PostgreSQL. `db.py` is the single file that manages the connection pool and provides every function that touches the database. No other file runs SQL directly; they all call functions in `db.py`.

### Key Imports

```python
import psycopg2                        # PostgreSQL driver for Python
from psycopg2.extras import RealDictCursor, execute_values  # Returns rows as dicts; bulk insert
from psycopg2 import pool              # ThreadedConnectionPool
from flask_login import UserMixin      # Makes subscriber objects work with Flask-Login auth
from werkzeug.security import generate_password_hash, check_password_hash  # Password hashing
import gspread                         # Google Sheets API (legacy backup)
from oauth2client.service_account import ServiceAccountCredentials  # Google auth
```

### The Connection Pool

PostgreSQL has a connection limit (typically 20–97 depending on the hosting plan). If every incoming webhook opened its own database connection, you'd exhaust that limit instantly under load.

The solution is a **connection pool**: a pre-opened set of connections that get borrowed and returned. Here is how it works:

1. At startup, `_get_pool()` creates a `psycopg2 ThreadedConnectionPool` with a minimum of 2 and a maximum of 20 connections.
2. A `threading.Semaphore(20)` is created alongside the pool. The semaphore acts as a ticket booth — only 20 callers can hold a ticket at once.
3. When code calls `get_db_connection()`, it first tries to acquire the semaphore (a ticket). If all 20 tickets are taken, it waits up to 10 seconds. If it still can't get one, it falls back to opening a direct connection.
4. Once a connection is borrowed, the code must call `return_db_connection(conn)` when done. This returns the connection to the pool and releases the semaphore so the next waiter can proceed.
5. Every function that uses the database wraps its work in a `try/finally` block to guarantee the connection is returned even if an exception is raised.

**The rule that must never be broken:** Every `get_db_connection()` call must have a matching `return_db_connection()` in a `finally` block. A missed return leaks a connection. Under load, leaked connections will exhaust the pool and bring the app down.

### The Schema (17 tables)

The tables are created by `init_db()` which runs automatically at startup. Key tables:

- **`subscribers`** — The master user table. Each row is one agency. Stores their email, GHL OAuth tokens, config JSON (carriers, bot name, prompt customization), Stripe IDs, API key, Discord tokens, and everything else about their account. The primary key is `location_id` (GHL's identifier for their CRM location).
- **`contact_messages`** — Every SMS message ever sent or received for every lead, keyed by `contact_id`. This is the conversation history.
- **`contact_facts`** — Structured facts extracted from conversations (name, age, coverage situation, etc.), keyed by `contact_id`.
- **`processed_webhooks`** — A simple table of webhook IDs that have already been processed. Used for deduplication.
- **`webhook_logs`** — Activity log entries shown in the dashboard's Logs tab.
- **`persistent_alerts`** — Dashboard banner alerts that survive page reloads.
- **`api_usage_logs`** — One row per external API request, used for rate limiting and analytics.
- **`call_history`** — Records of every phone call made or received.
- **`ai_minute_balances`** / **`ai_minute_purchases`** / **`ai_minute_usage_logs`** — The AI Minutes usage billing system.
- **`discord_connections`** / **`discord_servers`** / **`discord_webhook_channels`** — Discord integration state per user.

---

## 4. The Memory System (memory.py)

**File:** `memory.py`

### Why It Exists

The LLM has no built-in memory. Every API call is stateless — it only knows what you put in the prompt. `memory.py` bridges that gap by storing and retrieving conversation history and facts from the database, then formatting them for inclusion in the prompt.

### Key Imports

```python
from openai import OpenAI  # OpenAI SDK, used with xAI's base URL
from db import get_db_connection, return_db_connection  # Connection pool access
from psycopg2.extras import execute_values  # Efficient bulk inserts
import httpx  # HTTP client (used internally by the OpenAI SDK)
```

### Core Functions

**`save_message(contact_id, message_text, message_type)`**
Saves one message to `contact_messages`. Uses `ON CONFLICT DO NOTHING` so if the lead sends the same text twice, it doesn't crash or create a duplicate.

**`get_recent_messages(contact_id, limit=None)`**
Fetches conversation history from the database. If `limit=None`, fetches all messages (used by the narrative observer for unlimited context). If `limit=int`, fetches the most recent N exchanges. Returns a list of dicts like `{"role": "lead", "text": "..."}`.

**`save_new_facts(contact_id, facts)`**
Saves structured facts about a lead in bulk. Also uses `ON CONFLICT DO NOTHING` for deduplication, so the same fact can be discovered multiple times without creating duplicate rows.

**`get_known_facts(contact_id)`**
Returns all known facts about a contact as a formatted list string.

**`get_narrative(contact_id)`**
Returns the AI-generated narrative summary of the contact's story (generated by the narrative observer).

**`update_narrative(contact_id, messages)`**
Runs all conversation messages through the LLM to generate or update the narrative summary. This summary captures the emotional arc of the conversation: what the lead has shared, what they care about, where they are in the sales journey.

---

## 5. The AI Pipeline (tasks.py)

**File:** `tasks.py` (~49KB)

### Why It Exists

This is the background processing engine. Everything that cannot happen during the HTTP request-response cycle (because it's too slow) runs here, inside RQ worker jobs.

### Key Imports

```python
from openai import OpenAI            # xAI Grok API client
from db import ...                   # All database functions needed
from memory import save_message, save_new_facts  # Message/fact storage
from sales_director import generate_strategic_directive  # Conversation stage analysis
from age import calculate_age_from_dob  # Age calculation utility
from prompt import build_system_prompt  # Prompt builder
from ghl_message import send_sms_via_ghl  # GHL SMS delivery
from reply_sanitizer import sanitize_reply  # LLM output safety filter
from llm_caller import generate_clean_reply  # xAI API wrapper
from ghl_calendar import consolidated_calendar_op  # Calendar booking
from ghl_api import fetch_targeted_ghl_history, get_valid_token, fetch_contact_data_from_ghl
from contact_validator import validate_and_resolve_contact  # Contact ID resolution
```

### Booking Detection

The `detect_booking_request()` function is one of the most sophisticated parts of the system. It determines whether the lead is trying to book an appointment, using multiple signals:

- **Explicit keywords**: "book", "schedule", "let's do", "sign me up", etc.
- **Bot offered times**: Did the previous bot message contain structured time references like "2:00 PM" or "Tuesday at 4"?
- **Acceptance phrases**: "yes", "sure", "sounds good", "that works", etc. (but only when the bot had just offered times).
- **Rejection guard**: "can't do 2pm", "that doesn't work" — these prevent false bookings.
- **Long message guard**: Acceptance phrases in messages longer than 60 characters or containing question marks are not counted as acceptances.

The function also resolves ambiguity: if the bot offered "Tuesday at 2pm or Wednesday at 10am" and the lead says "the 2 one", the time-matching logic extracts "2:00 pm Tuesday" as the actual booking time.

### Message Batching

If a lead sends three texts in rapid succession ("hey" / "yeah" / "I'm interested"), the webhook fires three times. Without batching, the bot would respond three times to three separate messages. With batching:

1. The worker saves the first message to the database.
2. It sleeps for 3 seconds.
3. It queries the database for all consecutive unanswered lead messages.
4. It combines them into one: "hey. yeah. I'm interested"
5. The bot responds once to the combined message.

### The Sales Director

Before building the prompt, `generate_strategic_directive()` is called. This function analyzes the full conversation history and produces:

- **Stage**: QUALIFYING, DISCOVERY, IMPORTANCE, BOOKING, or CLOSED
- **Tactical narrative**: Plain-English coaching for the AI about what to do next
- **Recent exchanges**: The last N messages formatted for the prompt
- **Profile string**: Known facts about the lead formatted for the prompt
- **Underwriting context**: Any health/coverage details relevant to the current stage
- **Company context**: Any carrier-specific knowledge if the lead mentioned a carrier

This separation of concerns means the prompt builder doesn't need to make any decisions — the sales director already figured out what the AI should do.

---

## 6. The System Prompt (prompt.py)

**File:** `prompt.py`

### Why It Exists

The system prompt is the AI's instruction manual for every conversation. It tells the LLM who it is, how to behave, what to say, what never to say, and what the goal of the conversation is.

### Key Imports

```python
from insurance_knowledge import POLICY_KNOWLEDGE  # 70+ insurance policy knowledge blocks
```

### What the Prompt Covers

The `build_system_prompt()` function assembles a prompt from multiple sections:

- **Core mindset** (`CORE_UNIFIED_MINDSET`) — Persona, tone rules, forbidden phrases, forbidden punctuation (no em dashes, no bullet points, no exclamation marks — AI tells). The bot texts like a real human.
- **Privacy rules** — Never mention home addresses. Never assume coverage details unless the lead told you.
- **Cold vs. follow-up vs. inbound logic** — The instructions change dramatically based on whether this is the first contact, a follow-up with no reply, or an inbound response from the lead.
- **Discovery framework** — Find out the lead's situation before offering solutions. Never quote prices.
- **The importance question** — Before booking, get the lead to articulate *why* fixing their coverage gap matters to them. The person who says the reason out loud is the person who shows up to the appointment.
- **Carrier list** — Only the carriers the agent has contracted with are included.
- **Conversation history** — The last N exchanges formatted for context.
- **Known facts** — What the system knows about the lead.
- **Tactical narrative** — The sales director's guidance for this specific message.
- **Calendar slots** — Available appointment times (only injected when stage is booking).

---

## 7. The LLM Caller (llm_caller.py)

**File:** `llm_caller.py`

### Why It Exists

Every LLM call in the system routes through this single wrapper function. This ensures consistent model selection, error handling, retry logic, and the safety filter.

### Key Details

- **Model**: `grok-4-1-fast-non-reasoning` — This is deliberately the non-reasoning variant. Reasoning models (like o1/o3 or Grok's reasoning mode) produce internal chain-of-thought text before their final answer. If that thinking text leaked into an SMS to a lead, it would be a catastrophic failure. The non-reasoning model produces only the final output.
- **API endpoint**: The OpenAI Python SDK is used, but pointed at `https://api.x.ai/v1` as the base URL. xAI's API is OpenAI-compatible, so the same SDK works for both.
- **Retry**: If the first call produces a contaminated reply (caught by the sanitizer), the function retries once with a simplified prompt. The retry uses a minimal prompt to reduce the chance of contamination.
- **Safety net**: Even on the non-reasoning model, `sanitize_reply()` is called on every output as a final check.

---

## 8. Reply Safety (reply_sanitizer.py)

**File:** `reply_sanitizer.py`

### Why It Exists

The most catastrophic thing that can happen is the AI's internal reasoning text getting sent to a lead as an SMS. Imagine a lead receiving: "The user is asking about price. I should redirect to booking. Let me think about the best way to phrase this..." That would instantly reveal the bot.

The sanitizer is the last line of defense before any text reaches a lead.

### How It Works

**`sanitize_reply(reply)`:**
1. Strips `<thinking>...</thinking>` XML tags (used by reasoning models).
2. Checks for **contamination markers**: a list of 60+ phrases that only appear in system prompts or internal reasoning, such as "TCPA", "STOP CONDITIONS", "CRITICAL PRIVACY RULE", "underwriting context", "tactical narrative", etc. If 2 or more markers are found, the reply is blocked.
3. Length check: if a reply is over 500 characters AND contains any contamination marker, it's blocked.
4. If blocked, returns `None`.

**`is_safe_to_send(reply)`:**
A harder kill switch. Checks for an **absolute kill list** of phrases that should never appear in a message to a lead under any circumstances. Logs at ERROR level if triggered. Returns `False` to prevent sending.

If a reply is blocked by the sanitizer, the code falls back to a safe generic message or attempts a retry with a simpler prompt.

---

## 9. Contact Resolution (contact_validator.py)

**File:** `contact_validator.py`

### Why It Exists

GHL webhooks sometimes arrive with missing, null, or placeholder `contact_id` values. Without a valid contact ID, the bot cannot look up the conversation history, and any reply would go to the wrong person — a catastrophic privacy failure.

The validator resolves the identity of the lead using multiple fallback strategies.

### The 5-Step Resolution Process

`validate_and_resolve_contact(payload)` tries each step in order until it finds a valid contact ID:

1. **contact_id from payload** — If it exists, is at least 5 characters, and is not a placeholder ("unknown", "null", "test", etc.), use it.
2. **Phone + first name** — Search GHL for a contact matching the phone number, then verify the name matches (99% confidence). If the name doesn't match, log a CRITICAL-level warning with a 🚨 emoji (the "Dennis bug" — responding to the wrong Dennis).
3. **Phone only** — Search GHL by phone number without name verification.
4. **Name only** — Search GHL by name. This is risky for common names (five people named "John" in the same location) and logs a warning.
5. **Reject** — If none of the above work, return `None`. The webhook is rejected with `{"status": "error", "reason": "invalid_contact_id"}`.

---

## 10. The Voice Bridge (voice_bridge.py)

**File:** `voice_bridge.py` (~193KB)

### Why It Exists

In addition to SMS, the system supports real-time AI voice conversations. This module implements the bridge between Twilio's phone call infrastructure and xAI's Realtime voice API.

### The Audio Problem

Twilio sends phone audio in **mulaw (μ-law) format at 8,000 Hz** (8kHz). This is the standard for telephone networks — it's designed to sound good on a phone line while using minimal bandwidth.

xAI's Realtime API expects and produces **PCM 16-bit audio at 16,000 Hz** (16kHz). This is higher quality digital audio.

These formats are incompatible and must be transcoded in real time during the call.

### Key Imports

```python
import audioop       # Python standard library: mulaw<->PCM conversion (ulaw2lin, lin2ulaw)
import numpy as np   # Numerical arrays for audio sample manipulation
import soxr          # High-quality polyphase sinc resampler (anti-aliased 8kHz↔16kHz)
import scipy.signal  # Butterworth low-pass filter for phone-line warmth EQ
import websockets    # Async WebSocket client for xAI Realtime API
from flask_sock import Sock  # Synchronous WebSocket server for Twilio
import asyncio       # Python async framework for concurrent audio relay
import base64        # Encoding audio as base64 for JSON transport
import struct        # Binary packing for WAV file generation
```

### How the WebSocket Bridge Works

The voice bridge maintains two simultaneous WebSocket connections during a live call:

**Connection 1 (Twilio → App):** Twilio opens a WebSocket to `wss://yourserver.com/voice/stream`. This uses Flask-Sock, which handles WebSocket connections in a synchronous thread. Twilio sends JSON messages with base64-encoded mulaw audio chunks continuously during the call.

**Connection 2 (App → xAI):** The bridge opens a WebSocket to `wss://api.x.ai/v1/realtime` using the `websockets` library (async). This is the xAI Realtime API session.

Because one connection is sync (Flask-Sock) and the other is async (websockets), the bridge runs an asyncio event loop inside the Flask-Sock thread to bridge the two.

### Audio Flow: Twilio → xAI (Inbound from Lead)

```
Lead speaks → Twilio mic → mulaw 8kHz base64 → JSON message to /voice/stream
→ audioop.ulaw2lin() → PCM 16-bit at 8kHz
→ soxr.resample(8000 → 16000) → PCM 16-bit at 16kHz (anti-aliased, high quality)
→ base64 encode → JSON input_audio_buffer.append → xAI Realtime API
```

### Audio Flow: xAI → Twilio (AI Response to Lead)

```
xAI generates speech → PCM 16kHz base64 delta chunks → App receives via WebSocket
→ scipy.signal.lfilter() → Low-pass Butterworth filter at 3,400 Hz
   (removes the bright synthetic shimmer that makes AI voices sound robotic)
→ soxr.resample(16000 → 8000) → PCM 16-bit at 8kHz (anti-aliased)
→ audioop.lin2ulaw() → mulaw 8kHz
→ base64 encode → JSON media event → Twilio → Lead's phone
```

The low-pass filter is a key detail. Standard telephone bandwidth tops out at ~3,400 Hz. Frequencies above that don't exist on real phone calls. When xAI generates speech at 16kHz, it includes high-frequency components that make the voice sound artificially bright or tinny on a phone. The Butterworth filter removes those components, making the AI voice sound like it came through a real phone line.

### Call Flow: Inbound Call

1. Someone calls the agency's Twilio number.
2. Twilio POSTs to `POST /voice/inbound` with the caller's number.
3. The app looks up which subscriber owns that number.
4. The app responds with **TwiML XML** (Twilio's instruction language): `<Connect><Stream url="wss://..."/></Connect>`. This tells Twilio to open a WebSocket to the app and stream the call audio.
5. Twilio opens the WebSocket to `/voice/stream`.
6. The Flask-Sock handler picks it up and calls `run_voice_stream(ws)`.
7. `run_voice_stream` opens a connection to xAI's Realtime API.
8. Two async tasks run concurrently: `receive_from_twilio()` and `receive_from_xai()`, constantly transcoding and relaying audio in both directions.

### Call Flow: Outbound Call

1. The dashboard or a CRM automation calls `POST /voice/outbound-call`.
2. The app calls Twilio's REST API to create an outbound call from the agency's number to the lead.
3. When the lead answers, Twilio fetches TwiML from `POST /voice/outbound-twiml`.
4. That TwiML connects the call to the same WebSocket stream.
5. The bridge takes over exactly as in the inbound flow.

### Answering Machine Detection (AMD)

If AMD is enabled, Twilio runs machine detection when the call connects. Twilio then POSTs to `POST /voice/amd-status` with one of:

- `human` or `not_sure` — Call continues.
- `machine_start`, `fax` — Hang up immediately (no voicemail opportunity).
- `machine_end_beep` or `machine_end_silence` — The beep passed, leave a voicemail.

### Tool Calling During Voice

The AI voice agent can call three tools during a live call:

- **`check_calendar_availability`** — Fetches open slots from the GHL calendar in real time.
- **`book_appointment`** — Books an appointment during the call.
- **`transfer_to_agent`** — Triggers a live call transfer to a human agent.

When the AI decides to call a tool, xAI sends a `response.function_call_arguments.done` event. The bridge executes the tool, sends the result back to xAI as a `conversation.item.create` message, and triggers a new `response.create` to continue the conversation.

### Live Listen

Supervisors can listen to a live call in real time from the dashboard. The `/voice/listen-stream` WebSocket endpoint subscribes to the call's audio feed. During the call, every audio chunk put into `_call_listeners[call_sid]` by the main bridge is relayed to all listener sockets.

### Fast Startup Optimization

To minimize the delay before the AI says the first word, the bridge uses a two-phase prompt strategy:

1. **Phase 1 (immediate):** Configure xAI with a minimal prompt (just the name and a few lines). Send the greeting message immediately.
2. **Phase 2 (background):** While the AI is speaking the greeting, build the full system prompt (which requires loading conversation history, calendar slots, known facts — database calls). Once ready, update the xAI session with the full prompt via `session.update`. This is transparent to the lead.

---

## 11. The External API (api_v1.py)

**File:** `api_v1.py`

### Why It Exists

Agencies may use CRMs other than GHL (Ringy, Salesforce, custom systems) or want to build their own integrations. The external API lets any system send leads to InsuranceGrokBot using the same JSON format as the OpenAI chat completions API. This makes it easy to integrate — any tool that can talk to OpenAI can talk to this API with minimal changes.

### Authentication

Every request must include an `Authorization: Bearer sk_live_...` header. The key is validated as follows:

1. Check that the key starts with `sk_live_`.
2. Look up the subscriber by API key prefix in the database.
3. Run `secrets.compare_digest(provided_key, stored_key)` for **constant-time comparison**. Regular string equality (`==`) is vulnerable to timing attacks — an attacker can measure response time to figure out which characters of the key are correct. `compare_digest` always takes the same amount of time regardless of how much of the key matches.
4. If the key is wrong, wait 0.5 seconds before responding to slow down brute-force attempts.

### Rate Limiting

Rate limiting is implemented without Redis using a sliding window count on the `api_usage_logs` table. Every request is logged. Before processing, a `SELECT COUNT(*)` counts requests in the last 60 seconds. If the count exceeds the limit (default 120 RPM), the request is rejected with 429.

### How a Request Is Processed

1. Validate the API key.
2. Check the rate limit.
3. Parse the JSON body (OpenAI format: `contact_id`, `messages`, `first_name`, etc.).
4. Verify the subscriber has an `outbound_webhook_url` configured (this is where the AI's reply will be delivered).
5. Build a normalized payload in the same format that `process_webhook_task()` expects.
6. Enqueue the payload to the `production` RQ queue.
7. Return HTTP 202 Accepted with a job ID.
8. The worker processes the message and POSTs the reply to the `outbound_webhook_url`.

---

## 12. The Web Server (main.py)

**File:** `main.py` (~6,700 lines)

### Why It's So Big

`main.py` contains every HTTP route in the application. Flask routes don't easily split across files (the Blueprint system exists for this, and the voice and API routes do use Blueprints), but the vast majority of routes — dashboard, GHL OAuth, Stripe, admin — live directly in `main.py`.

### Key Setup at the Top

```python
from openai import OpenAI       # xAI API client
from rq import Queue            # Redis job queue
from flask_sock import Sock     # WebSocket support
from voice_bridge import voice_bp, run_voice_stream, run_listen_stream
```

**xAI client:**
```python
client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
```
The OpenAI Python SDK is used because xAI's API is OpenAI-compatible. Just swap the base URL.

**Redis queues:**
```python
def ensure_redis():
    global redis_conn, q_production, q_demo
    redis_conn = redis.from_url(REDIS_URL)
    redis_conn.ping()
    q_production = Queue("production", connection=redis_conn)
    q_demo = Queue("demo", connection=redis_conn)
```
`ensure_redis()` is called before every Redis operation. If Redis drops (network blip, restart), the next call will reconnect automatically. This is more resilient than connecting once at startup and failing silently later.

### Session Cookie Configuration

```python
app.config['SESSION_COOKIE_SAMESITE'] = 'None'
app.config['SESSION_COOKIE_SECURE'] = True
```

InsuranceGrokBot can be embedded inside GHL's iframe. Browsers block cookies from iframes by default (SameSite=Lax). Setting `SameSite=None` with `Secure=True` allows the session cookie to work inside an iframe over HTTPS.

### Headers for VoIP

```python
@app.after_request
def add_iframe_headers(response):
    response.headers.pop('X-Frame-Options', None)
    response.headers['Permissions-Policy'] = 'microphone=*, camera=*, autoplay=*'
    response.headers['Content-Security-Policy'] = "frame-ancestors *"
    return response
```

- `X-Frame-Options` is removed so GHL can iframe the app.
- `frame-ancestors *` (CSP) replaces it more broadly.
- `Permissions-Policy: microphone=*` is required for the browser to allow microphone access inside the iframe for the VoIP dialer.

### PII Redaction

```python
class PIIRedactionFilter(logging.Filter):
    def filter(self, record):
        # Redact phone numbers and email addresses from all log messages
```

This logging filter is applied to every log handler at module startup. Every phone number and email address is replaced with `[PHONE]` or `[EMAIL]` in logs. This prevents production logs from containing personally identifiable information.

---

## 13. Background Workers (worker.py)

**File:** `worker.py`

### Why It Exists

Flask handles HTTP requests in threads. Heavy processing (calling the LLM, fetching GHL history, booking appointments) takes 5–30 seconds. If that ran inside the HTTP request handler, the thread would be blocked for 30 seconds, Gunicorn would run out of threads, and incoming webhooks would queue up and eventually time out.

Workers are separate Python processes that read jobs from Redis and execute them independently of the web server.

### How It Works

```bash
python worker.py production  # runs one worker reading from the "production" queue
python worker.py demo        # runs one worker reading from the "demo" queue
```

At startup:
1. Reads the queue name from `sys.argv[1]`.
2. Connects to Redis, calls `redis.ping()`. If Redis is unreachable, exits with code 1 rather than starting in a broken state.
3. Generates a unique worker name: `worker-production-a3f9b21c` (queue name + 8 hex chars from `uuid.uuid4()`). This name appears in Redis and log output, making it easy to identify which worker processed a specific job.
4. Creates an `rq.Worker` instance and calls `.work()` — an infinite loop that polls Redis for jobs and runs them.

The Procfile runs 4 production workers and 1 demo worker simultaneously, for 5 parallel workers total.

---

## 14. CRM Integration (ghl_api.py, crm_adapters/)

### GoHighLevel (Primary CRM)

**`ghl_api.py`** handles all direct API calls to GHL:
- `get_valid_token(location_id)` — Returns a valid OAuth access token, automatically refreshing it if expired.
- `fetch_contact_data_from_ghl(contact_id, location_id, token)` — Fetches full contact details from GHL.
- `fetch_targeted_ghl_history(contact_id, location_id, token, limit)` — Fetches conversation history from GHL to sync into the local database.
- `search_contact_by_phone()` / `search_contact_by_name()` — Used by the contact validator.

GHL uses OAuth 2.0. Each subscriber has their own `access_token` and `refresh_token`. Access tokens expire; `get_valid_token()` checks the expiry and calls the refresh endpoint if needed, then updates the database.

### CRM Adapters

The `crm_adapters/` directory contains a factory pattern for non-GHL CRMs. When a subscriber's `crm_type` is not `ghl`, the adapter factory returns the appropriate adapter:

- **HubSpot** — `crm_adapters/hubspot.py`
- **Salesforce** — `crm_adapters/salesforce.py`
- **Pipedrive** — `crm_adapters/pipedrive.py`
- **Zoho** — `crm_adapters/zoho.py`
- **Insureio** — `crm_adapters/insureio.py`
- **Zapier** — `crm_adapters/zapier.py`

Each adapter implements the same interface: `get_free_slots()` and `book_appointment()`. The pipeline in `tasks.py` calls these interchangeably.

---

## 15. Billing and Subscriptions

### Stripe Subscriptions

Three subscription tiers:
- **Individual Plan** — Single agency, standard features.
- **Agency Starter** — Agency owner + limited sub-users.
- **Agency Pro** — Agency owner + more sub-users + advanced features.

Stripe fires webhook events to `POST /stripe-webhook` when subscriptions are created, updated, or cancelled. The app verifies the webhook signature using `STRIPE_WEBHOOK_SECRET`, then updates the subscriber's status in the database.

### AI Minutes

Voice calls consume xAI API credits at a rate proportional to call duration. The AI Minutes system lets subscribers prepurchase a balance of "AI minutes" to pay for voice usage.

When a call ends, the `/voice/status` callback calculates the call duration and deducts from the subscriber's `ai_minute_balances` row via `deduct_ai_minutes()`.

Subscribers can purchase AI minutes packages via the Billing tab in the dashboard. Packages are defined by `AI_MINUTES_PRICE_ID_*` environment variables.

---

## 16. Discord Integration

### Why It Exists

Agency owners often run team Discord servers for internal communication. The dashboard embeds a Discord chat panel so agents never need to switch tabs to check messages.

### How It Works

1. **OAuth Connect** — The user clicks "Connect Discord" in the dashboard. They're redirected to Discord's OAuth flow at `/discord/connect`. After authorizing, Discord redirects to `/discord/callback`, where the app exchanges the code for tokens and stores them in `discord_connections`.
2. **Server Setup** — Users add up to 3 Discord servers. The app's bot must be invited to each server to read and post messages.
3. **Panel** — The Discord panel slides out from the sidebar. Channels are listed from the saved servers.
4. **Polling** — Messages are polled every 4 seconds when the panel is open. A background poll every 45 seconds checks for unread messages to update the notification bell badge.
5. **Send** — The user types a reply in the panel; the app POSTs it to Discord via the bot token.

### Panel Positioning

The Discord panel is positioned with CSS:
```css
#discordPanel {
    position: fixed;
    left: var(--sidebar-width);
    transform: translateX(-100%);  /* hidden off-screen to the left */
}
#discordPanel.open {
    transform: translateX(0);      /* slides in from the sidebar edge */
}
body.sidebar-collapsed #discordPanel {
    left: var(--sidebar-collapsed-width);  /* adjusts when sidebar collapses */
}
```
When the panel opens, `body.discord-open` is added, which shifts the main content area right to make room.

---

## 17. Security Model

### Authentication

- **Dashboard login** — Email + password via Flask-Login. Passwords hashed with Werkzeug's `generate_password_hash` (pbkdf2:sha256 by default).
- **Password reset** — `itsdangerous.URLSafeTimedSerializer` generates a signed token with a 1-hour expiry. The token is emailed to the user. If tampered with or expired, it is rejected.
- **CSRF protection** — Flask-WTF adds CSRF tokens to all forms. Requests without a valid token are rejected.
- **Session cookies** — `SameSite=None; Secure` so the dashboard works inside GHL iframes over HTTPS.

### API Key Security

- Keys are stored hashed in the database.
- Authentication uses `secrets.compare_digest()` for constant-time comparison.
- A 0.5-second delay is added on failed attempts to slow brute-force attacks.
- Keys must start with `sk_live_` to be accepted.

### Webhook Verification

- **GHL webhooks**: HMAC-SHA256 signature verified against `MARKETPLACE_WEBHOOK_SECRET`.
- **Stripe webhooks**: Stripe's own signature verification via `STRIPE_WEBHOOK_SECRET`.

### Admin Access

The `ADMIN_EMAILS` environment variable contains a whitelist of email addresses that have access to God Mode (`/admin/god-mode`). This list is never stored in the database — it lives only in the environment. Even if someone gained database write access, they could not add themselves to the admin list.

### PII Protection

The `PIIRedactionFilter` on all log handlers means phone numbers and email addresses never appear in logs. This protects against log aggregation services accidentally storing PII.

---

## 18. Environment Variables Reference

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Flask session signing key — must be a long random string |
| `DATABASE_URL` | PostgreSQL connection string (`postgresql://user:pass@host/db`) |
| `REDIS_URL` | Redis connection (`redis://localhost:6379`) |
| `XAI_API_KEY` | xAI API key (used for SMS LLM + voice Realtime API) |
| `GHL_CLIENT_ID` / `GHL_CLIENT_SECRET` | GHL public marketplace OAuth app credentials |
| `GHL_PRIVATE_CLIENT_ID` / `GHL_PRIVATE_CLIENT_SECRET` | GHL private app credentials |
| `MARKETPLACE_WEBHOOK_SECRET` | HMAC secret for verifying GHL webhook signatures |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | Twilio master account (manages sub-accounts) |
| `TWILIO_API_KEY` / `TWILIO_API_SECRET` | Twilio API key for generating browser VoIP tokens |
| `TWILIO_TWIML_APP_SID` | TwiML App SID for browser-based VoIP |
| `STRIPE_SECRET_KEY` | Stripe secret key for API calls |
| `STRIPE_PUBLISHABLE_KEY` | Stripe publishable key (safe to expose in browser) |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signature verification secret |
| `STRIPE_PRICE_ID` | Stripe price ID for Individual plan |
| `STRIPE_AGENCY_STARTER_PRICE_ID` | Stripe price ID for Agency Starter plan |
| `STRIPE_AGENCY_PRO_PRICE_ID` | Stripe price ID for Agency Pro plan |
| `AI_MINUTES_PRICE_ID_*` | Stripe price IDs for each AI Minutes package |
| `MAIL_SERVER` / `MAIL_PORT` / `MAIL_USERNAME` / `MAIL_PASSWORD` | SMTP settings for outbound email |
| `MAIL_DEFAULT_SENDER` | From address for system emails |
| `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` | Discord OAuth app credentials |
| `DISCORD_BOT_TOKEN` | Discord bot token for reading/posting messages |
| `DISCORD_REDIRECT_URI` | Discord OAuth callback URL |
| `ADMIN_EMAILS` | Comma-separated list of admin email addresses |
| `SUBSCRIPTION_PRICE` | Monthly price displayed on marketing pages (default: 97) |
| `GOOGLE_CREDENTIALS` | JSON service account credentials for Google Sheets backup |
| `SUBSCRIBER_SHEET_EDIT_URL` | Google Sheets URL for legacy subscriber backup |
| `DB_POOL_MAX` | Max database connections in pool (default: 20) |
| `DB_POOL_WAITERS` | Max requests waiting for a pool connection (default: 500) |
| `DB_POOL_TIMEOUT` | Seconds to wait for a pool slot before direct connect (default: 10) |
| `API_RATE_LIMIT_RPM` | External API rate limit in requests per minute (default: 120) |

---

## 19. Troubleshooting Guide

### The Bot Stopped Responding

**Step 1: Check the activity logs.** In the dashboard, go to the Logs tab. Look for recent entries. If the last webhook shows "queued" or "received" but no "reply sent," the worker may be stuck.

**Step 2: Check Redis.** If workers can't connect to Redis, jobs pile up. Check that the Redis URL is correct and the Redis service is running.

**Step 3: Check the xAI API key.** If `XAI_API_KEY` is invalid or expired, every LLM call will fail. The worker logs will show authentication errors from the xAI API.

**Step 4: Check the GHL OAuth tokens.** If the GHL access token expired and cannot be refreshed (e.g., the refresh token was revoked), `get_valid_token()` will fail and the worker will abort with "token refresh failed". The subscriber needs to reconnect GHL OAuth from the dashboard.

**Step 5: Check for TCPA opt-outs.** If the lead sent "stop" at any point, the bot will not respond to that contact. This is intentional and correct. To re-enable, the lead must opt back in through the proper channel.

### Duplicate Responses Sent to a Lead

The `processed_webhooks` table deduplicates by webhook ID. If duplicates are occurring, check:
- Whether two separate webhook configurations in GHL are sending to the same endpoint.
- Whether the webhook ID field is being populated correctly in the incoming payload.

### Wrong Person Getting Messages (The Dennis Bug)

If the contact validator can't find a contact by ID and falls back to name search, it might match the wrong person with a common name. Look in worker logs for `CRITICAL` level messages with the 🚨 emoji. These indicate a name mismatch was detected.

The solution is to ensure GHL always includes the `contact_id` in webhook payloads by checking the webhook configuration in GHL settings.

### Database Connection Errors Under Load

If you see "connection pool wait timed out" in logs, the pool is exhausted. Options:
- Increase `DB_POOL_MAX` if your PostgreSQL plan allows more connections.
- Reduce worker count to lower DB pressure.
- Audit for connection leaks (code paths that call `get_db_connection()` without a `finally: return_db_connection(conn)` block).

### Voice Calls Connecting But No AI Audio

The most common causes:
1. `XAI_API_KEY` is invalid — the xAI WebSocket connection fails silently.
2. The HTTPS URL for the WebSocket stream is misconfigured — Twilio requires `wss://`, and the host must match the actual server hostname.
3. The voice feature is not enabled in the subscriber's dashboard (the `voice_config.enabled` flag must be true).

### Voice AI Sounds Robotic or Tinny

The Butterworth low-pass filter is applied to cut frequencies above 3,400 Hz. If the voice sounds unusually tinny, confirm that `scipy` and `soxr` installed correctly. Run `python -c "import soxr; import scipy.signal"` in the app environment.

### Dashboard Won't Load Inside GHL Iframe

The app requires `SESSION_COOKIE_SAMESITE=None` and `SESSION_COOKIE_SECURE=True`. These settings only work over HTTPS. If the server is running on HTTP (development), the session cookie will not be sent in the iframe and the user will appear logged out on every request.

### Jinja2 Template Errors on Startup

Flask loads and validates all templates at startup. A single unclosed `{% block %}`, mismatched `{% if %}`, or invalid `{{ variable }}` expression will crash the startup. The error message will include the template filename and line number. Check any recently modified `.html` file carefully.

### Worker Jobs Not Processing

Check that workers are actually running:
```bash
ps aux | grep worker.py
```

Check the RQ dashboard (if installed) or query Redis directly:
```bash
redis-cli llen rq:queue:production
```

If jobs are piling up but workers are running, check for exceptions in worker logs. A Python import error in any file imported by `tasks.py` will cause every worker job to fail immediately.

### Google Sheets Connection Error on Startup

The Google Sheets integration is a legacy backup and is non-fatal. If `GOOGLE_CREDENTIALS` is not set or the service account lacks access, the startup logs will show an error but the app will continue normally. This can be safely ignored unless you need the Google Sheets backup.

### Stripe Webhooks Not Processing

Stripe signs its webhooks. If `STRIPE_WEBHOOK_SECRET` is wrong or missing, every Stripe webhook will be rejected with a signature verification error. When updating the Stripe webhook secret, make sure to use the secret from the specific webhook endpoint in the Stripe dashboard (each endpoint has its own secret).

### External API Rate Limit Too Aggressive

If legitimate integrations are hitting the 429 rate limit, increase `API_RATE_LIMIT_RPM` in the environment. The default is 120 requests per minute. Be aware this is per API key, so multiple keys from the same subscriber each get their own limit.

---

*This manual is generated from the actual codebase. If something in here contradicts what the code does, trust the code and update this document.*
