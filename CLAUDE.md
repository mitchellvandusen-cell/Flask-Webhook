# CLAUDE.md — InsuranceGrokBot Flask-Webhook App

## What This App Is

**InsuranceGrokBot** is a white-label AI-powered SMS and voice bot platform specifically built for insurance agents. It connects to GoHighLevel (GHL/Lead Connector) CRM via OAuth, intercepts incoming webhook events (new leads, SMS messages, etc.), and uses xAI's Grok LLM to generate intelligent, context-aware replies — automatically sent back through Twilio as white-label SMS (users never see "Twilio" branding).

The system is multi-tenant SaaS: each subscribing insurance agency gets their own isolated bot instance with their own phone numbers, carrier list, prompt configuration, and conversation history. It also supports agency owners managing multiple sub-accounts.

---

## Architecture Overview

```
Browser / GHL Webhook
        │
        ▼
Gunicorn (4 threads) ──► Flask app (main.py, ~6700 lines)
        │
        ├── PostgreSQL (psycopg2 threaded pool, 2–20 connections)
        ├── Redis + RQ (background job queues)
        ├── Twilio (SMS send/receive, Voice, sub-accounts)
        ├── xAI Grok API (LLM for text replies + Realtime API for voice)
        ├── Stripe (subscriptions + AI Minutes usage billing)
        ├── GoHighLevel OAuth (CRM data access)
        └── Discord (embedded team chat in dashboard)
```

### Worker Architecture

```
Deployment (Render / Railway):
  Flask-Webhook:  gunicorn main:app --worker-class gthread --threads 40 --timeout 14400
  worker:         python worker.py production        (webhook processing + AI intelligence)
  worker-bg:      python worker.py website demo      (GHL sync, backfill, demo chat)
  Redis:          managed Redis instance
```

### RQ Queues

| Queue | Worker | Purpose | Tasks |
|-------|--------|---------|-------|
| `production` | `worker` | Fast webhook processing + AI analysis | `process_webhook_task` (120s), `analyze_contact_intelligence_task` (30s), `analyze_contacts_batch_task` (600s) |
| `website` | `worker-bg` | Long-running GHL sync + recovery | `run_incremental_sync_all` (30m), `deep_sync_conversations` (2h), `backfill_failed_webhooks` (10m) |
| `demo` | `worker-bg` | Demo chat isolation | `process_webhook_task` for demo contacts (120s) |

All queues are defined in `extensions.py` and initialized via `ensure_redis()`. Worker startup: `python worker.py <queue1> [queue2] ...` (defaults to `production` if no args).

---

## Core Files

| File | Purpose | Size |
|------|---------|------|
| `main.py` | Flask app — all HTTP routes, Redis/RQ setup, Flask-Login/Mail/Sock | ~6700 lines |
| `db.py` | PostgreSQL layer — connection pool, all data access functions, schema | ~97KB |
| `tasks.py` | Background job engine — AI pipeline, webhook processing | ~49KB |
| `voice_bridge.py` | Twilio ↔ xAI Realtime voice WebSocket bridge | ~193KB |
| `worker.py` | RQ worker startup script | small |
| `api_v1.py` | External API blueprint (`/api/v1/`) — OpenAI-compatible | small |
| `prompt.py` | System prompt builder for LLM context | medium |
| `memory.py` | Contact memory/facts retrieval | medium |
| `individual_profile.py` | Comprehensive contact profile builder | medium |
| `contact_validator.py` | Contact ID validation/resolution | small |
| `reply_sanitizer.py` | Sanitize/clean LLM replies before sending | small |
| `llm_caller.py` | Clean LLM invocation wrapper | small |
| `twilio_provisioning.py` | Twilio sub-account provisioning, number management, Trust Hub, A2P 10DLC registration | medium |
| `carrier_list.py` | 63 insurance carriers for UI picker | small |
| `insurance_companies.py` | 270+ carrier names/aliases for AI detection | small |
| `insurance_knowledge.py` | Deep product knowledge (term, whole, IUL, FE) for AI context | medium |
| `underwriting.py` | Live carrier underwriting rules from Google Sheets | medium |
| `conversation_engine.py` | Conversation stage analysis, objection classification | medium |
| `sales_director.py` | Strategic directive generator for AI pipeline | medium |
| `age.py` | DOB-to-age calculator for contact profiles | small |
| `ghl_api.py` | GHL OAuth token management + API helpers | small |
| `ghl_calendar.py` | GHL calendar booking operations | medium |
| `ghl_message.py` | GHL SMS delivery | small |
| `ghl_logger.py` | Log IGB messages/calls to GHL via Conversation Provider API | small |
| `webhook_delivery.py` | Outbound webhook delivery with HMAC signing + retries | small |
| `send_email_api.py` | Mailgun API email sender | small |
| `utils.py` | JSON serialization helpers + AI reply cleaning | small |
| `sync_subscribers.py` | Syncs subscriber DB from external source at startup | small |
| `ghl_sync.py` | GHL data sync engine — pulls conversations, opportunities, phone numbers into local DB | ~900 lines |
| `twilio_sms.py` | Direct Twilio SMS sender — bypasses GHL API, drop-in replacement for ghl_message.py | small |
| `lead_intelligence.py` | AI-powered lead intelligence via xAI Grok micro-prompts with caching | medium |
| `voice/predictive_engine.py` | Erlang-C pacing, TCPA compliance tracker, agent state machine, callback queue, timezone/consent lookup | medium |
| `crm_adapters/` | CRM adapter factory + GHL/HubSpot/Salesforce/Pipedrive/Zoho/Insureio/Zapier | directory |

---

## Environment Variables (`.env.example`)

### Flask / Core
- `SESSION_SECRET` — Flask session secret (code also checks `SECRET_KEY` as fallback)
- `YOUR_DOMAIN` — Public domain URL (default: `http://localhost:8080`)
- `DATABASE_URL` — PostgreSQL connection string
- `REDIS_URL` — Redis connection (default: `redis://localhost:6379`)

### GoHighLevel OAuth (CRM)
- `GHL_CLIENT_ID`, `GHL_CLIENT_SECRET` — Public marketplace app credentials
- `GHL_BASE_URL` — API base (default: `https://services.leadconnectorhq.com`)
- `MARKETPLACE_WEBHOOK_SECRET` — Signature verification for GHL webhooks
- `PRIVATE_APP_CLIENT_ID`, `PRIVATE_APP_SECRET_ID` — Private app credentials (fallback only)

### AI / xAI Grok
- `XAI_API_KEY` — xAI API key (used for both text and voice Realtime API)

### Twilio
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` — Master account (manages sub-accounts per user)
- `TWILIO_PHONE_NUMBER` — Master fallback phone number
- `TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET` — For browser-based calling (Access Tokens)

### Stripe
- `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`
- `STRIPE_WEBHOOK_SECRET` — For validating Stripe webhook events
- `STRIPE_PRICE_ID` — Individual plan price ID
- `STRIPE_PRO_DIALER_PRICE_ID` — Pro Dialer plan price ID ($224.99/mo)
- `STRIPE_AGENCY_STARTER_PRICE_ID`, `STRIPE_AGENCY_PRO_PRICE_ID`
- `AI_MINUTES_PRICE_ID_500`, `AI_MINUTES_PRICE_ID_2000`, `AI_MINUTES_PRICE_ID_5000`, `AI_MINUTES_PRICE_ID_10000` — Usage-based AI minutes packages

### Email
- `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER`, `MAIL_USE_TLS`, `MAIL_USE_SSL`
- `MAILGUN_API_KEY`, `MAILGUN_DOMAIN` — For API-based sending via `send_email_api.py`

### Google Sheets (subscriber sync)
- `GOOGLE_CREDENTIALS` — JSON service account credentials
- `SUBSCRIBER_SHEET_URL` — CSV export URL for subscriber sync
- `SUBSCRIBER_SHEET_EDIT_URL` — Spreadsheet edit URL

### Google Calendar (optional)
- `GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_CALENDAR_CLIENT_SECRET`, `GOOGLE_CALENDAR_REDIRECT_URI`

### Discord
- `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` — Discord OAuth app credentials
- `DISCORD_BOT_TOKEN` — Bot token for reading/posting messages
- `DISCORD_REDIRECT_URI` — OAuth callback URL

### Slack (optional)
- `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_REDIRECT_URI`

### Cron / Scheduled Jobs
- `CRON_SECRET` — Shared secret for authenticating cron endpoints (passed as `?key=` query param or `Authorization: Bearer` header)

### Railway Build Config (not used in app code)
- `NIXPACKS_PYTHON_VERSION` — Python version for Railway builds

---

## Database Schema (21 tables)

All tables created in `db.py`'s `init_db()` function:

| Table | Purpose |
|-------|---------|
| `subscribers` | Master user table: `location_id` PK, email, OAuth tokens, config JSON, Stripe customer/subscription IDs, `sms_send_via` |
| `agency_billing` | Agency owner billing records (includes `sms_send_via`) |
| `contact_messages` | Chat history per GHL contact |
| `contact_facts` | NLP-extracted facts about contacts (spaCy) |
| `processed_webhooks` | Webhook deduplication (idempotency) |
| `contact_narratives` | AI-generated narrative summaries per contact |
| `webhook_logs` | Activity/audit log per location |
| `persistent_alerts` | Dashboard banner alerts (DB-backed, survive page reloads) |
| `marketplace_installs` | GHL marketplace app install tracking |
| `api_usage_logs` | External API key usage for rate limiting and analytics |
| `call_history` | Voice call records |
| `ai_minute_balances` | Per-user AI minute credit balance |
| `ai_minute_purchases` | AI minutes purchase history |
| `ai_minute_usage_logs` | AI minutes usage history |
| `discord_connections` | Discord OAuth tokens per user |
| `discord_servers` | Saved Discord servers per user (max 3) |
| `discord_webhook_channels` | Webhook-connected Discord channels |
| `ghl_conversations` | Synced GHL message history (contact_id, message_type, direction, body, source, ghl_message_id UNIQUE) |
| `ghl_opportunities` | Synced GHL pipeline/deal data (pipeline_name, stage_name, status, monetary_value, ghl_opportunity_id UNIQUE) |
| `ghl_sync_state` | Sync progress tracking (resource_type, last_sync_at, last_cursor, sync_status, total_synced) |
| `contact_intelligence` | AI intelligence cache (contact_id PK, analysis JSONB, analyzed_at — invalidated on new messages) |

### Database Connection Pool
- `ThreadedConnectionPool` with semaphore queuing
- `DB_POOL_MIN=2`, `DB_POOL_MAX=20`, `DB_POOL_WAITERS=500`, `DB_POOL_TIMEOUT=10s`
- All DB calls use `get_db_connection()` / `return_db_connection()` — always return connections in `finally` blocks
- Google Sheets legacy backup connection attempted at startup (non-fatal if fails)

---

## HTTP Routes (main.py)

### Public / Marketing
- `GET /` — Home page
- `GET /comparison`, `/comparison/text-drip` — Comparison pages
- `GET /getting-started` — Setup guide
- `GET /reviews` — Reviews page (POST submits review)
- `GET /contact`, `/privacy`, `/terms`, `/disclaimers`
- `GET /faq`, `/integrations`, `/support`, `/setup-guide`
- `GET /demo-chat` — Demo chatbot UI

### Authentication
- `GET|POST /register` — New user registration
- `GET|POST /login` — User login (Flask-Login)
- `GET /logout` — Logout
- `GET|POST /forgot-password` — Password reset request
- `GET|POST /reset-password/<token>` — Password reset (itsdangerous token, 1hr expiry)
- `GET|POST /set-password` — Set password for claim-account flow
- `GET|POST /claim-account` — Agency sub-user claim flow

### Dashboard
- `GET|POST /dashboard` — Main dashboard (requires login + active subscription)
- `GET /onboarding-status` — Setup progress checklist
- `POST /save-profile` — Save operator name/bio

### GHL OAuth
- `GET /oauth/initiate` — Start GHL OAuth flow (redirects to GHL authorization URL)
- `GET /oauth/callback` — GHL OAuth callback (exchanges code for tokens, stores in DB)
- `GET /oauth/loading` — Loading page while OAuth completes
- `GET /refresh` — Manually refresh GHL OAuth tokens
- `GET /app` — App landing page post-install

### GHL Webhooks
- `POST /webhook` — Main GHL webhook receiver (queues jobs via RQ)
- `POST /webhook/app-installed` — GHL marketplace app install webhook

### Stripe
- `POST /stripe-webhook` — Stripe webhook handler (subscription lifecycle)
- `GET /checkout` — Individual plan checkout
- `GET /checkout/agency-starter` — Agency Starter checkout
- `GET /checkout/agency-pro` — Agency Pro checkout
- `GET /cancel` — Subscription cancelled page
- `GET /success` — Subscription success page
- `POST /create-portal-session` — Stripe billing portal
- `POST /a2p/checkout` — A2P 10DLC registration fee checkout ($19 via Stripe)

### AI Minutes
- `GET /ai-minutes/balance` — Check balance
- `GET /ai-minutes/packages` — Available packages
- `POST /ai-minutes/checkout` — Purchase AI minutes
- `GET /ai-minutes/usage` — Usage history

### API Endpoints (internal dashboard)
- `GET /api/logs` — Activity logs (paginated)
- `GET|POST /api/alerts` — Persistent alerts
- `POST /api/alerts/<id>/dismiss` — Dismiss an alert
- `GET|POST /api/save-config` — Save SMS bot configuration
- `GET|POST /api/voice-config` — Voice configuration
- `GET|POST /api/carriers` — Contracted carriers list
- `GET|POST /api/bot-settings` — Bot settings (tone, behavior)
- `POST /api/generate-key` — Generate external API key
- `POST /api/revoke-key` — Revoke external API key
- `POST /api/webhook-url` — Set outbound webhook URL
- `GET /api/api-status` — Check external API status/usage
- `GET /api/fetch-calendars` — Fetch GHL calendars
- `GET|POST /api/integrations/save` — Save CRM integration settings
- `POST /api/integrations/test` — Test CRM integration
- `GET /api/onboarding-check` — Check onboarding completion status

### Demo
- `POST /demo/chat` — Demo chat endpoint
- `POST /demo/init` — Initialize demo session
- `POST /demo/reset` — Reset demo session
- `POST /api/demo/reset` — API version of demo reset
- `GET /get-logs` — Real-time demo logs SSE stream
- `GET /download-transcript` — Download demo chat transcript

### Discord
- `GET /discord/connect` — Start Discord OAuth
- `GET /discord/callback` — Discord OAuth callback
- `GET /discord/disconnect` — Disconnect Discord
- `GET /api/discord/status` — Discord connection status
- `GET /api/discord/guilds` — User's Discord guilds
- `GET /api/discord/bot-invite/<guild_id>` — Bot invite URL
- `GET /api/discord/bot-check/<guild_id>` — Check if bot joined
- `GET|POST /api/discord/servers` — Saved Discord servers
- `GET /api/discord/channels/<guild_id>` — List channels
- `GET|POST /api/discord/messages/<channel_id>` — Read/send messages

### Admin (God Mode — ADMIN_EMAILS whitelist)
- `GET /admin/god-mode` — Admin dashboard (view all subscribers)
- `POST /admin/impersonate/<email>` — Impersonate a user
- `POST /admin/revert` — Revert impersonation
- `GET /admin/god-mode/logs/<location_id>` — View user's logs
- `GET /admin/god-mode/subscriber/<location_id>` — View subscriber details
- `GET|POST /api/admin/send-email` — Send admin email
- `GET /api/admin/marketplace-installs` — View marketplace installs
- `POST /api/admin/marketplace-installs/<id>/send-setup-email`
- `POST /api/admin/marketplace-installs/send-all-setup-emails`
- `GET|POST /api/admin/discover-installs` — Discover new installs

### Agency
- `GET|POST /agency-dashboard` — Agency owner command center (sidebar+tab layout with KPIs, agents, call log, activity, settings, billing)
- `GET|POST /agency-login` — Agency sub-user login
- `POST /api/agency/invite-sub-user` — Invite sub-user
- `POST /api/agency/resend-invite` — Resend invite email
- `POST /api/agency/invite-all` — Invite all pending sub-users
- `GET /api/agency/logs/<location_id>` — Agency member logs
- `GET /api/agency/kpis?period=<today|week|month|all>` — Aggregated KPIs across all sub-accounts (calls, connected, rate, duration, messages, active agents, daily/hourly charts, prior period comparison)
- `GET /api/agency/agent-stats?period=<today|week|month|all>` — Per-agent stats breakdown (calls, connected, talk time, avg duration, messages, last call)
- `GET /api/agency/call-log?limit=&offset=&agent=` — Paginated call log across all agents with optional agent filter

### GHL Data Sync & Inbox
- `POST /api/cron/sync-ghl-data` — Trigger incremental GHL data sync (queued via RQ; auth via `CRON_SECRET`)
- `GET /api/ghl-phone-numbers` — Fetch GHL phone numbers (cached or live from API)
- `GET /api/inbox/conversations` — Unified conversation list from synced GHL data (paginated)
- `GET /api/inbox/thread/<contact_id>` — Full conversation thread for a contact with pipeline stage
- `GET /api/stream/notifications` — Server-Sent Events (SSE) endpoint for real-time dashboard events
- `GET /api/contact/<contact_id>/intelligence` — AI-powered intelligence dossier (summary, temperature, score, actions, facts, pipeline)
- `GET /api/sync-status` — GHL sync status for dashboard display

### Cron
- `GET|POST /api/cron/send-reminders` — Send onboarding reminder emails
- `GET|POST /api/cron/refresh-tokens` — Proactive OAuth token refresh (refreshes tokens expiring within 2 hours; designed for 15-minute cron interval)
- `GET|POST /api/cron/recover-failed-webhooks` — Find webhook tasks that failed due to token errors in the last N hours, get fresh token, re-queue them (schedule every 15 min; auth via `CRON_SECRET`)
- `GET|POST /api/cron/backfill-failed-webhooks` — One-shot backfill: recover webhooks that failed before `failed_webhook_payloads` table existed, reconstruct payloads from `webhook_logs`, re-queue (safe to run multiple times; auth via `CRON_SECRET`)

### Website Bot
- `POST /website-bot-webhook` — External website chatbot webhook

### External API (Blueprint, api_v1.py)
- `POST /api/v1/chat/completions` — OpenAI-compatible chat completions endpoint (Bearer token auth, 120 req/min rate limit)

### Training API (Blueprint, api_v1.py — `trn_` token auth)
- `GET /api/v1/training/validate` — Verify training token is valid, return account info
- `GET /api/v1/training/recordings` — Paginated call recordings with transcripts (limit, offset, since, direction params)
- `GET /api/v1/training/recordings/<call_sid>` — Single recording detail by call SID
- `GET /api/v1/training/recordings/<call_sid>/audio` — Stream MP3 recording (training-token authenticated, proxies from Twilio)
- `GET /api/v1/training/stats` — Summary statistics (total calls, with_recording, with_transcript, durations)

### Voice (Blueprint, voice_bridge.py)
- Voice WebSocket routes for Twilio ↔ xAI Realtime API bridging
- `POST /voice/transfer-complete` — Twilio action callback when transfer `<Dial>` ends; returns `<Hangup/>` to release parent call
- `GET /voice/stats?period=<today|week|month|all>` — Aggregated call statistics (KPIs, duration buckets, daily/hourly breakdown, top contacts); uses subscriber's timezone
- `GET /voice/contact-call-counts?ids=<csv>` — Batch local call counts for up to 300 contact IDs
- `GET /voice/contact/<id>/ghl-call-count` — Merged call count: local dialer DB + GHL conversation calls (covers GHL-native + WAVV + dialer)
- `GET /voice/call-history` — Paginated call history for current user (limit/offset params, max 200)
- `POST /voice/transcribe-recording` — On-demand recording transcription: downloads MP3 from Twilio, sends to xAI Whisper, saves transcript to call_history.transcript. Body: `{call_sid, recording_url}`
- `GET /voice/contact-intelligence-bulk?ids=<csv>` — Bulk cached AI intelligence for Smart Filters (zero AI cost, reads from cache only). Returns `{cached: {id: {temperature, score, summary}}, uncached: [ids]}`
- `POST /voice/contact-intelligence-analyze` — Batch-analyze uncached contacts via AI (up to 5 per request). Body: `{contact_ids: [...]}`

### Multi-Line Dialer (Blueprint, voice/dialer.py)
- `POST /voice/multi-dial` — Initiate up to 4 concurrent calls (Pro Dialer tier required). Body: `{contacts: [{contact_id, phone, first_name}], dial_mode, max_lines}`
- `GET /voice/active-lines` — Current active call lines count and details for the user
- `POST /voice/multi-hangup` — Hang up multiple active calls at once. Body: `{call_sids: [...]}`
- `POST /voice/multi-status` — Batch poll status of multiple calls. Body: `{call_sids: [...]}`
- `GET /voice/predictive-stats` — 7-day predictive dialing analytics (connect rate, recommended lines, dial ratio); Erlang-C algorithm for `predictive_dialer` tier
- `GET /voice/compliance` — TCPA compliance dashboard (abandon rate, DNC violations, hours violations, compliance score); `predictive_dialer` tier only
- `GET|POST /voice/agent-state` — Agent state machine (Ready/Not Ready/Wrap-Up/Break); `predictive_dialer` tier
- `GET|POST|DELETE /voice/callback-queue` — Callback queue management (schedule, view, cancel re-dials)
- `GET /voice/recording-consent` — Check two-party recording consent by phone area code
- `POST /voice/recording-consent/batch` — Batch consent check for up to 300 numbers
- `GET /checkout/predictive-dialer` — Predictive Dialer plan checkout ($349.98/mo)

### Billing / Plan Management
- `GET /checkout/pro-dialer` — Pro Dialer plan checkout ($224.99/mo)
- `POST /change-plan` — Switch between Power Dialer and Pro Dialer plans (Stripe proration). Body: `{target_tier: "individual"|"pro_dialer"}`
- `GET /subscription-info` — Current subscription tier, max lines, features (JSON)

### A2P 10DLC (Blueprint, voice_bridge.py)
- `GET /voice/a2p/status` — Current A2P registration state from voice_config JSONB
- `POST /voice/a2p/register-brand` — Submit brand for Twilio A2P vetting (sub-users gated by payment)
- `GET /voice/a2p/brand-status` — Poll brand vetting progress from Twilio
- `POST /voice/a2p/create-campaign` — Create campaign + messaging service + link phone numbers
- `GET /voice/a2p/campaign-status` — Poll campaign approval status
- `POST /voice/a2p/import` — Import externally-approved brand + campaign IDs (CNP migration)
- `POST /voice/a2p/mark-fee-paid` — Mark A2P registration fee as paid for sub-account users

---

## Core Processing Pipeline (tasks.py)

When a GHL webhook arrives at `POST /webhook`:
1. Signature verification + deduplication (check `processed_webhooks` table)
2. Job queued to RQ `production` or `demo` queue
3. Worker runs `process_webhook_task()`
4. Pipeline: fetch contact from GHL → load conversation history → extract facts (spaCy) → build system prompt → call xAI Grok API → sanitize reply → send SMS via Twilio → log everything

### Key behaviors
- Strict `contact_id` validation prevents cross-contamination between contacts
- White-label SMS: Twilio sub-accounts mask Twilio branding from end users
- AI Minutes: each LLM call deducts from the user's minute balance
- Carrier awareness: bot knows only the carriers the agent has contracted

---

## Voice Bridge (voice_bridge.py)

- **Flask Blueprint** `voice_bp` mounted in main.py
- Bidirectional WebSocket bridge: Twilio mulaw 8kHz ↔ xAI PCM 16kHz
- Audio pipeline: soxr resampling + audioop mulaw/PCM conversion + scipy Butterworth EQ
- In-memory call tracking: `active_calls`, `transfer_requests`, `call_listeners`
- Supports real-time voice AI conversations using xAI's Realtime API
- A2P 10DLC registration routes for brand/campaign compliance (see A2P 10DLC section below)

---

## Multi-Line Dialer (voice/dialer.py)

### What It Is
Enterprise multi-line power dialer that allows Pro Dialer subscribers ($224.99/mo) to initiate up to 4 concurrent outbound calls simultaneously. Includes predictive dialing that uses AI-calculated dial ratios based on historical connect rates.

### Architecture
- **Subscription gating**: `POST /voice/multi-dial` checks `subscription_tier == 'pro_dialer'` or admin status before allowing multi-line calls
- **Backend**: All multi-line routes in `voice/dialer.py` — `multi_dial()`, `get_active_lines()`, `multi_hangup()`, `multi_call_status()`, `predictive_stats()`
- **Frontend**: Multi-line engine in `static/js/dashboard/dialer.js` — `_multiLineActive` Map tracks concurrent calls, `multiLineDialBatch()` fires batches, `multiLinePollAll()` batch-polls status
- **In-memory tracking**: Uses same `active_calls` dict as single-line; multi-line calls tagged with `_multi_line: True`
- **Predictive pacing**: `GET /voice/predictive-stats` calculates connect rate from 7-day call history, recommends optimal line count (1-4)

### Key Routes
| Route | Method | Purpose |
|-------|--------|---------|
| `POST /voice/multi-dial` | POST | Initiate up to 4 concurrent calls |
| `GET /voice/active-lines` | GET | Current active lines count + details |
| `POST /voice/multi-hangup` | POST | Hang up multiple calls at once |
| `POST /voice/multi-status` | POST | Batch poll status of multiple calls |
| `GET /voice/predictive-stats` | GET | 7-day connect rate, recommended lines, dial ratio |

### Predictive Dialer Algorithm
**Pro Dialer** (simple formula):
1. Query `call_history` for last 7 days of calls
2. Calculate connect rate: `connected_calls / total_calls * 100`
3. Compute dial ratio: `min(4.0, max(1.0, 100 / connect_rate))`
4. Recommend lines: `round(dial_ratio)`, capped at 4

**Predictive Dialer** (Erlang-C M/M/N queue model):
1. Query `call_history` for 7-day stats (connect rate, avg handle time, avg talk time)
2. Model as M/M/N queue: arrival rate = agents × ratio × answer rate / ring time
3. Binary search (20 iterations) for max dial ratio keeping predicted abandon rate ≤ 3%
4. TCPA auto-throttle: if current rolling 30-day abandon rate > 2.4%, reduce ratio by factor
5. Uses `_erlang_c_probability()` with log-space arithmetic for numerical stability
6. Phone number parsing: strips digits-only, removes country code `1` for 11-digit numbers

### Frontend State
- `_multiLineActive` (Map) — tracks all concurrent call SIDs with contact info
- `_multiLineEnabled` (bool) — set from subscription tier check on page load
- `_multiLineMaxLines` (int) — max concurrent lines (1 for individual, 4 for pro_dialer)
- `_multiLineConnectedSid` (string) — which call the agent is currently interacting with
- `_predictiveStats` (object) — cached predictive analytics from server

### Multi-Line Dialer Settings (voice_config JSONB)
All settings stored in `voice_config` JSONB on `subscribers` table, validated in `blueprints/dashboard.py`, enforced server-side in `voice/dialer.py`.

| Setting | Key | Default | Range | Enforcement |
|---------|-----|---------|-------|-------------|
| Max concurrent lines | `max_lines_setting` | 3 | 1-4 | Server: caps batch size in `multi_dial()` |
| Wrap-up time (seconds) | `wrap_up_time` | 15 | 0-120 | Client: timer between batches in `multiLinePollAll()` |
| Require disposition | `require_disposition` | true | bool | Client: gates next batch until all completed calls dispositioned |
| Calling hours start | `calling_hours_start` | "08:00" | HH:MM | Server: `_check_calling_hours()` with pytz timezone |
| Calling hours end | `calling_hours_end` | "21:00" | HH:MM | Server: supports midnight wrap-around (e.g., 22:00-06:00) |
| Same-number cooldown | `same_number_cooldown_hours` | 4 | 0-72 | Server: `_check_cooldown_and_daily_max()` batch SQL |
| Daily max per contact | `same_contact_daily_max` | 3 | 0-10 | Server: batch SQL with `make_interval()` |
| On-machine action | `on_machine_action` | "hangup" | hangup/voicemail_drop/continue | Client: handles AMD result in poll |
| Auto-disposition no-answer | `auto_disposition_no_answer` | true | bool | Client: auto-marks terminal no-answer calls |
| Auto-disposition voicemail | `auto_disposition_voicemail` | true | bool | Client: auto-marks terminal voicemail calls |
| Max abandon rate % | `max_abandon_rate_pct` | 3.0 | 1.0-10.0 | FTC 3% safe harbor for TCPA compliance |

### Server-Side Enforcement
- `_check_calling_hours(voice_config, agent_tz_str)` — validates current time against configured window, supports midnight wrap-around
- `_check_cooldown_and_daily_max(location_id, phones, voice_config, conn)` — batch SQL using `make_interval(hours => %s)` for proper psycopg2 parameterization
- Both functions called in `dial_contact()` (single-line) and `multi_dial()` (multi-line)
- Blocked contacts returned with `calling_hours_blocked` or `cooldown_blocked` status

### DASHBOARD_BOOT Integration
Settings injected from server to client via `window.DASHBOARD_BOOT` in `dashboard.html`. Client variables use optional chaining (`window.DASHBOARD_BOOT?.settingName ?? default`) to safely handle missing boot data.

### Plan Switching
- `POST /change-plan` — Stripe subscription modification with proration (supports individual ↔ pro_dialer ↔ predictive_dialer)
- `GET /subscription-info` — Returns tier, max_lines, features for billing UI
- Dashboard billing tab shows all three plans with "Change Plan" button
- Plan change updates `subscription_tier` in DB and triggers frontend re-init

### Predictive Dialer Engine (voice/predictive_engine.py)
Enterprise-only module (`subscription_tier = 'predictive_dialer'`) with:
- **Phone number parsing**: `area_code_to_timezone(phone)`, `area_code_to_state(phone)` — digits-only extraction, US country code removal for 11-digit numbers, ~300 NANP area code mappings
- **Timezone enforcement**: `check_recipient_timezone(phone, start, end)` — pytz-based, midnight wrap-around support
- **Recording consent**: `is_two_party_consent_state(phone)` — 12 two-party consent states (CA, CT, DE, FL, IL, MD, MA, MT, NV, NH, PA, WA)
- **Erlang-C pacing**: `calculate_optimal_dial_ratio()` — M/M/N queue model with binary search, TCPA throttle at 80% of limit
- **TCPA tracker**: `TCPAComplianceTracker` (global singleton `tcpa_tracker`) — thread-safe rolling 30-day abandon rate, auto-prune, DB bootstrap via `load_from_db()`
- **Agent state machine**: `AgentStateManager` (global singleton `agent_state_manager`) — ACD states (Ready/Not Ready/On Call/Wrap-Up/Break/Extended Away/Logged Out), auto wrap-up→ready transitions, predicted availability within N-second horizon
- **Callback queue**: `CallbackQueue` (global singleton `callback_queue`) — thread-safe scheduled re-dial queue with duplicate prevention, 24-hour auto-prune of completed/cancelled items
- **Compliance metrics**: `get_compliance_metrics()` — aggregated compliance score (0-100) from TCPA, DNC violations, calling hours violations

---

## A2P 10DLC Compliance (twilio_provisioning.py + voice_bridge.py)

### What It Is
A2P 10DLC (Application-to-Person 10-Digit Long Code) is the carrier-mandated registration system for sending business SMS over standard phone numbers. Without registration, messages may be filtered or blocked by carriers.

### Architecture
- **State storage**: All A2P data stored in `voice_config["a2p"]` JSONB on the `subscribers` table — no new DB tables needed. Matches the existing pattern used by Trust Hub and Numbers.
- **Two flows**: (1) Register New — creates Trust Hub profile + Brand Registration + Campaign via Twilio APIs. (2) Import Existing — imports TCR-approved brand/campaign IDs from external providers (GHL/LeadConnector) via CNP migration.
- **Payment gate**: Sub-account users (`parent_agency_email IS NOT NULL`) must pay the A2P registration fee ($19) via Stripe before submitting. Agency owners and super admins bypass the gate.
- **Provisioning functions**: `twilio_provisioning.py` contains `create_a2p_brand()`, `get_a2p_brand_status()`, `create_messaging_service()`, `add_phone_to_messaging_service()`, `create_a2p_campaign()`, `get_a2p_campaign_status()`, `import_external_brand()`, `import_external_campaign()`.

### Registration Flow
1. User fills out brand form (business name, EIN, address, contact) → `POST /voice/a2p/register-brand` → Twilio Trust Hub + Brand Registration.
2. Twilio vets the brand (hours to days) → `GET /voice/a2p/brand-status` polls for approval.
3. User fills out campaign form (use case, sample messages, opt-in/out) and selects phone numbers → `POST /voice/a2p/create-campaign` → creates Messaging Service, links numbers, submits campaign.
4. Carrier review (hours to days) → `GET /voice/a2p/campaign-status` polls for approval.

### voice_config.a2p Schema
```json
{
  "brand_sid": "BN...",
  "brand_status": "APPROVED",
  "campaign_sid": "QE...",
  "campaign_status": "VERIFIED",
  "messaging_service_sid": "MG...",
  "a2p_fee_paid": true,
  "registered_at": "2026-02-24T..."
}
```

---

## GHL Data Sync Engine (ghl_sync.py)

- **Incremental sync**: Pulls conversations, opportunities, phone numbers, and location data from GoHighLevel into local Postgres tables via paginated API calls.
- **Enterprise retry**: `_api_get()` with exponential backoff, 429 rate limit handling, and automatic 401 token refresh.
- **3 sync targets**: `ghl_conversations` (messages), `ghl_opportunities` (pipeline/deals), phone numbers.
- **Cursor tracking**: `ghl_sync_state` table stores last sync timestamp, cursor position, and status per resource type per location.
- **Query functions**: `get_merged_call_count()`, `get_merged_call_history()`, `get_contact_pipeline_stage()`, `get_sync_stats_for_dashboard()`, `get_conversation_stats()`.
- **Cron trigger**: `POST /api/cron/sync-ghl-data` queues `sync_all_for_location()` jobs via RQ.

---

## SMS Routing (twilio_sms.py + tasks.py)

- **Dual-channel SMS**: Users choose between GHL (default) and direct Twilio for outbound SMS via `sms_send_via` column.
- **`twilio_sms.py`**: Drop-in replacement for `ghl_message.py`. Returns same 3-tuple `(success, fail_reason, http_detail)`. Includes deduplication, safety filtering, max-retry with backoff.
- **Pipeline routing**: `tasks.py` checks `subscriber.sms_send_via` — if it starts with `+`, routes through `send_sms_via_twilio()`; otherwise uses GHL. Falls back to GHL if Twilio creds missing.
- **Config UI**: Radio picker in Bot Config with LeadConnector logo for GHL numbers and robot icon for IGB/Twilio numbers.

---

## GHL Conversation Provider Sync (ghl_logger.py)

GHL stays the source-of-truth CRM. When messages are sent or calls are made through IGB (via Twilio), they are logged back to GHL's conversation threads using the GHL Conversation Provider API.

### Conversation Providers (GHL Marketplace App)
Two providers are registered in the GHL Marketplace App under Modules > Conversation Providers:

| Provider | Type | ID | Delivery URL |
|----------|------|-----|-------------|
| InsuranceGrokBot | Call | `699c83535fc465bbff87a78d` | `/voice/outbound-call` |
| InsuranceGrokBot SMS | Custom SMS | `699c84aef36d66cc10a56e82` | `/webhook` |

### Environment Variables
- `GHL_SMS_CONVERSATION_PROVIDER_ID` — Custom SMS provider ID (default: `699c84aef36d66cc10a56e82`)
- `GHL_CALL_CONVERSATION_PROVIDER_ID` — Call provider ID (default: `699c83535fc465bbff87a78d`)

### How Sync Works

**IGB → GHL (logging outbound activity)**:
- After bot sends SMS via Twilio (`tasks.py`) → `log_outbound_sms_to_ghl()` POSTs to `/conversations/messages/outbound` with `conversationProviderId`
- After agent sends SMS from dashboard (`voice_bridge.py`) → same flow
- After call completes (`voice_bridge.py` status callback) → `log_call_to_ghl()` POSTs to `/conversations/messages/outbound` (or `/inbound` for inbound calls)

**GHL → IGB (agent sends from GHL UI)**:
- Agent types message in GHL conversation under InsuranceGrokBot SMS tab → GHL fires webhook to `/webhook`
- `webhooks.py` detects Conversation Provider outbound webhook (has `messageId` + `type: "SMS"` + `phone`)
- Sends via Twilio using subscriber's sub-account credentials
- Returns result to GHL

### API Endpoints Used
- `POST /conversations/messages/outbound` — Log outbound SMS/calls to GHL (type: `Custom` for SMS, `Call` for calls)
- `POST /conversations/messages/inbound` — Log inbound SMS/calls to GHL

### Key Files
- `ghl_logger.py` — `log_outbound_sms_to_ghl()`, `log_inbound_sms_to_ghl()`, `log_call_to_ghl()`
- `blueprints/webhooks.py` — `_is_conversation_provider_outbound()`, `_handle_conversation_provider_outbound()`

---

## AI-Powered Lead Intelligence (lead_intelligence.py)

- **Single micro-prompt**: Gathers all contact context (messages, facts, pipeline, calls, tags, narrative) and sends one prompt to `grok-4-1-fast-non-reasoning`.
- **AI returns JSON**: summary (2-sentence snapshot), temperature (hot/warm/cool/cold + reason), score (0-100), and 2-4 next-best-actions with priority and FontAwesome icon.
- **Persistent caching**: Results cached in `contact_intelligence` table (JSONB). Cache invalidated ONLY when new messages arrive after the analysis (no time-based expiry). Once analyzed, classification persists until conversation changes. Repeat views cost zero.
- **Cost**: ~$0.001-0.003 per analysis (~200 output tokens).
- **Frontend**: Loading shimmer → temperature pill badge (fire/thermometer/snowflake icons) + score + AI summary + recommended actions panel.
- **Pipeline injection**: `tasks.py` injects pipeline stage into AI system prompt via `get_contact_pipeline_stage()`.
- **Bulk API**: `get_bulk_cached_intelligence(location_id, contact_ids)` returns cached AI data for many contacts in one DB query (zero AI cost, LEFT JOIN optimized). Used by Smart Filters on every dialer load.
- **RQ batch analysis**: `analyze_contacts_batch_task(location_id, contact_ids)` is an RQ task that processes uncached contacts on workers (not web threads). Enqueued in batches of 10 with 60s timeout.
- **Auto re-analysis**: After `process_webhook_task` processes a message, it auto-queues `analyze_contact_intelligence_task` to RQ so the contact's Smart Filter classification stays fresh without any user action.

### AI-Powered Smart Filters (Dialer)

The dialer's Smart Filters use AI intelligence to classify contacts — NOT simple rules like "customer responded = hot." The AI reads full conversation history before classifying. There are ZERO rule-based fallbacks — if AI hasn't analyzed a contact, it shows "pending" (score "?", neutral dots) instead of fake scores.

**AI output fields** (per contact):
- `temperature`: hot/warm/cool/cold — based on what the lead ACTUALLY SAID, not response timing
- `temperature_reason`: One sentence explaining the rating
- `score`: 0-100 likelihood to convert (a "not interested" lead gets 5-15, not 50+)
- `should_respond`: true/false — AI determines if the agent needs to act NOW (reads conversation)
- `should_respond_reason`: Why the agent should or should not respond
- `engagement_level`: 0-3 depth of interaction (0=no contact, 1=surface, 2=real conversation, 3=deep engagement)
- `summary`: 2-sentence situational snapshot with specific details
- `actions`: 2-4 specific next steps with FontAwesome icons

**Groups** (priority order):
1. **Should Respond** (red) — AI `should_respond=true` (lead is waiting for a reply, asked a question, expressed interest)
2. **Hot Leads** (green) — AI temperature = "hot" (actively buying, quoting, asking for coverage, ready to book)
3. **Warm Leads** (orange) — AI temperature = "warm" (engaged, sharing info, genuine interest)
4. **Cool** (blue) — AI temperature = "cool" (went quiet, slow/short replies, soft objections)
5. **Cold** (gray) — AI temperature = "cold" (said no/stop/not interested, ghosting — "no thanks" = COLD not hot)
6. **Do Not Contact** (red) — Opted out via DnD flag or stop keywords
7. **Analyzing...** (spinner) — Contacts pending AI analysis (auto-triggered in background)

**Data flow**:
1. Dialer loads → bulk engagement data fetched (`/voice/contact-engagement`)
2. Bulk AI intelligence fetched from cache (`/voice/contact-intelligence-bulk`) — zero AI cost
3. Contacts grouped by AI temperature into Smart Filter categories
4. Uncached contacts queued to RQ workers via `POST /voice/contact-intelligence-analyze` (batches of 10)
5. Frontend polls bulk endpoint every 4s — as workers complete analysis, contacts move from "Analyzing..." into correct group
6. Subsequent dialer loads are instant from cache (no time-based expiry)
7. When webhook processes a new message, `analyze_contact_intelligence_task` auto-queues to RQ — classification stays fresh

---

## External API (api_v1.py)

- Mounted at `/api/v1/`
- `POST /api/v1/chat/completions` — OpenAI-compatible format
- Bearer token authentication with constant-time comparison (`hmac.compare_digest`)
- Rate limit: `API_RATE_LIMIT_RPM` env var (default 120 req/min) using Redis sliding window
- Per-key usage logged to `api_usage_logs` table

### Training Integration API
- `trn_` token authentication via `@require_training_token` decorator (constant-time comparison, 60 req/min rate limit)
- Training tokens stored in `subscribers.voice_config` JSONB (`training_token`, `training_token_created_at`)
- Generated/revoked via dashboard: `POST /api/training/generate-code`, `POST /api/training/revoke-code`
- `GET /api/v1/training/recordings` — Paginated call records with absolute `audio_url` for MP3 download
- `GET /api/v1/training/recordings/<call_sid>/audio` — Streams MP3 from Twilio via training token auth (bypasses `@login_required` proxy)
- `GET /api/v1/training/stats` — Aggregate call statistics for the authenticated account
- Recording URLs: Stored as `/voice/recording/{recording_sid}` internally (proxy); training API returns absolute `audio_url` pointing to `/api/v1/training/recordings/{call_sid}/audio`

---

## Frontend (Dashboard)

### Template Structure
```
templates/
  dashboard.html              Main dashboard layout (individual user)
  agency-dashboard.html       Agency command center (sidebar+tab, self-contained, inline JS)
  dashboard/_head.html        <head>, all inline CSS (Discord, alerts, etc.)
  dashboard/_topbar.html      Top bar with page title and controls
  dashboard/_sidebar.html     Left collapsible sidebar with nav
  dashboard/_alerts.html      Flash messages + persistent alert banners
  dashboard/tabs/             Tab pane contents
    config.html               SMS Bot configuration
    voice.html                Voice configuration
    dialer.html               Voice dialer
    workflows.html            Automation workflows
    connect.html              CRM connection guide
    carriers.html             Contracted carriers picker
    advanced.html             Advanced settings
    aiminutes.html            AI Minutes balance/purchase
    billing.html              Billing/subscription management
    logs.html                 Activity logs
  dashboard/modals/
    discord_panel.html        Discord message panel (side panel, hidden)
    discord_modal.html        Discord server picker modal
    congrats.html             Congratulations overlay
    save_overlay.html         Saving indicator overlay
```

### JavaScript Modules
```
static/js/dashboard/
  sidebar.js        Sidebar nav, collapse, theme toggle
  save_config.js    Config form save + SMS channel picker (GHL/Twilio number loading)
  alerts.js         Persistent alert loading/dismiss
  logs.js           Activity log loading
  connect_crm.js    CRM connection UI
  voice.js          Voice config
  dialer.js         Voice dialer, iPhone app UI, call count badges, statistics panel,
                    AI-powered Smart Filters, bulk intelligence, Inbox app, SSE notifications
  numbers.js        Phone number management + A2P 10DLC registration UI
  ai_minutes.js     AI Minutes UI
  carriers.js       Carrier chip selection
  advanced.js       Advanced settings
  api_keys.js       External API key management
  discord.js        Discord integration (all Discord UI logic)
  tutorial.js       Interactive dashboard tutorial (driver.js, 11 chapters, glassmorphism UI)
```

### CSS
- `static/css/style.css` — Global styles (1457 lines)
- Inline `<style>` in `dashboard/_head.html` — All dashboard-specific styles including Discord panel, sidebar, alerts

### Key CSS Variables
```css
--sidebar-width: 260px
--sidebar-collapsed-width: 62px
--accent: #00ff88
--dark-bg: #050505
```

---

## Discord Integration

The dashboard embeds a Discord team chat panel:
1. User connects Discord via OAuth (`/discord/connect`)
2. User adds up to 3 Discord servers
3. Bot must be invited to each server
4. User clicks a channel in the sidebar → chat panel slides open
5. Messages polled every 4 seconds; background poll every 45 seconds for unread notifications
6. Users can read and reply to Discord messages without leaving the dashboard

### Discord Panel Position
- Fixed panel that slides out from the right side of the sidebar
- Toggle: click a channel in the sidebar, or use the Discord bell button in the topbar
- Dismissible with the X button in the panel header

---

## Security

- **PII Redaction**: All logs have phone numbers and emails redacted via `PIIRedactionFilter`
- **Flask-WTF CSRF**: All forms protected
- **OAuth CSRF protection**: State parameter with cryptographic nonce validated on callback
- **OAuth scope validation**: Requested scopes must match exactly what's approved on the GHL marketplace app (18 scopes). No PKCE — GHL rejects `code_verifier`.
- **itsdangerous tokens**: Password reset tokens expire in 1 hour
- **Constant-time comparison**: API key authentication uses `hmac.compare_digest`
- **Admin whitelist**: `ADMIN_EMAILS` controls god-mode access
- **Webhook signature verification**: GHL webhooks verified with `MARKETPLACE_WEBHOOK_SECRET`
- **Stripe webhook signature**: Verified with `STRIPE_WEBHOOK_SECRET`
- **Token encryption**: OAuth tokens encrypted at rest using Fernet symmetric encryption
- **Token-locked fields**: OAuth tokens hidden/readonly in UI after OAuth connection

---

## CRM Adapters

The `crm_adapters/` directory contains a factory pattern with adapters for:
- GoHighLevel (primary)
- HubSpot
- Salesforce
- Pipedrive
- Zoho
- Insureio
- Zapier

---

## Development Notes

### Running Locally
```bash
# Install dependencies
pip install -r requirements.txt
python -m spacy download en_core_web_md

# Set up .env (copy from .env.example)
cp .env.example .env
# Fill in required values

# Start web server
gunicorn main:app --worker-class gthread --threads 40 --timeout 14400

# Start workers (in separate terminals)
python worker.py production                # webhook processing + AI intelligence
python worker.py website demo              # GHL sync, backfill, demo chat
```

### Dependency Highlights
- `flask`, `gunicorn`, `werkzeug`
- `psycopg2-binary` — PostgreSQL driver
- `redis`, `rq` — Background job queue
- `flask-login`, `flask-wtf`, `flask-mail`, `flask-sock`
- `openai` — Used with xAI base URL (`https://api.x.ai/v1`)
- `spacy` + `en_core_web_md` — NLP for fact extraction
- `stripe` — Billing
- `twilio` — SMS and Voice
- `soxr`, `scipy` — Audio resampling for voice bridge
- `gspread`, `google-auth` — Legacy Google Sheets backup

### Testing / Admin
- God Mode: Log in with an email in `ADMIN_EMAILS`, then visit `/admin/god-mode`
- User impersonation available in god mode
- Demo chat available at `/demo-chat` (no auth required)

### Key Patterns
- All DB calls use `get_db_connection()` / `return_db_connection()` in try/finally
- Redis reconnection: `ensure_redis()` auto-reconnects on failure
- RQ jobs named `worker-{queue}-{uuid8}` for debugging
- spaCy model loaded once at module level in `tasks.py`
- `XAI_API_KEY` used with `OpenAI(api_key=..., base_url="https://api.x.ai/v1")`
- `update_subscriber_token()` uses `pg_advisory_xact_lock(hashtext(location_id))` before UPDATE to prevent token refresh race conditions across 4 parallel workers
- `update_crm_config_token(location_id, access_token)` in `db.py` uses JSONB `||` merge to update only `access_token` in `crm_config` without clobbering other fields
- CRM adapters (HubSpot, Salesforce, Zoho) call `update_crm_config_token()` after token refresh to persist to DB
- `ghl_message.py` auto-retries with refreshed token on HTTP 401/403; `_token_refreshed` flag prevents infinite retry loops
- Dashboard tabs (Bot Config, Voice Config, Connect/Integrations) use two-column side-menu layout with `switchConfigPanel()`, `switchVoicePanel()`, `switchConnectPanel()` JS functions
- `_showDashToast(ok, msg)` global utility injected by `dashboard.html` before all JS modules — used by all save functions for consistent bottom-right toast feedback
- On-demand recording transcription via `POST /voice/transcribe-recording` — downloads MP3 from Twilio, transcribes via xAI Whisper, saves to `call_history.transcript`
- `ghl_sync.py` uses `_api_get()` with exponential backoff + 429/401 handling for all GHL API calls
- `lead_intelligence.py` fires one AI micro-prompt per contact returning temperature, score, should_respond, engagement_level, summary, and actions; caches in `contact_intelligence` table; cache invalidated ONLY when new messages arrive (no time-based expiry). Bulk cache reads power Smart Filters; batch analysis auto-runs for uncached contacts
- SMS routing: `tasks.py` checks `sms_send_via` column — `'ghl'` routes through GHL API, `'+1...'` routes through `twilio_sms.py` direct sender
- Dialer uses iPhone-style app paradigm: `iosOpenApp()` / `iosGoHome()` for Messages, Calls, Voicemail, and Inbox apps
- `_loadContactIntelligence(contactId)` async-loads AI intelligence into `#igb-ai-summary`, `#igb-nba-section`, `#igb-pipeline-badge` placeholders in contact detail panel; also updates `_igbIntelCache` so Smart Filter groups re-render in real time

---

## Carrier List

`carrier_list.py` contains 63 insurance carriers as `{"key": "...", "name": "..."}` dicts for the dashboard chip picker UI. `insurance_companies.py` has 270+ carrier names including aliases for AI-powered carrier detection in conversations. The bot only references carriers the agent has selected.

---

## Subscription Tiers

- **Power Dialer (Individual)**: $149.99/month — single-line dialing, AI texting, AI voice, lead intelligence, Smart Filters, unlimited minutes, 5 sales frameworks. No contracts, cancel anytime. `subscription_tier = 'individual'`
- **Pro Dialer**: $224.99/month — everything in Power Dialer PLUS multi-line dialing (up to 4 concurrent lines), predictive dialer with AI-optimized pacing, connect rate analytics, priority queue. `subscription_tier = 'pro_dialer'`. Env var: `STRIPE_PRO_DIALER_PRICE_ID`
- **Predictive Dialer**: $349.98/month — everything in Pro Dialer PLUS Erlang-C predictive pacing, TCPA auto-throttle (rolling 3% abandon rate), recipient timezone enforcement (area code → timezone lookup), agent state machine (Ready/Not Ready/Wrap-Up/Break), compliance dashboard, recording consent tracking (two-party consent states), callback queue with scheduled re-dials. `subscription_tier = 'predictive_dialer'`. Env var: `STRIPE_PREDICTIVE_DIALER_PRICE_ID`
- **Agency Starter**: Agency owner + up to 14 sub-users, multi-tenant dashboard
- **Agency Pro**: Agency owner + unlimited sub-users + dedicated queue + white-glove onboarding
- **AI Minutes**: Add-on usage-based billing for AI voice processing

Subscriptions managed via Stripe. Users without active subscriptions see a paywall on the dashboard. Plan switching between Power Dialer, Pro Dialer, and Predictive Dialer is handled via `POST /change-plan` which calls `stripe.Subscription.modify()` with proration.
