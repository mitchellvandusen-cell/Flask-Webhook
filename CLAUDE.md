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
Procfile:
  web:           gunicorn main:app --threads 40 --timeout 0
  worker-prod-1..4: python worker.py production   (4 parallel RQ workers)
  worker-demo:      python worker.py demo          (1 demo worker)
```

Workers run `process_webhook_task()` from `tasks.py` asynchronously. This is the core AI processing pipeline.

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
| `webhook_delivery.py` | Outbound webhook delivery with HMAC signing + retries | small |
| `send_email_api.py` | Mailgun API email sender | small |
| `utils.py` | JSON serialization helpers + AI reply cleaning | small |
| `sync_subscribers.py` | Syncs subscriber DB from external source at startup | small |
| `ghl_sync.py` | GHL data sync engine — pulls conversations, opportunities, phone numbers into local DB | ~900 lines |
| `twilio_sms.py` | Direct Twilio SMS sender — bypasses GHL API, drop-in replacement for ghl_message.py | small |
| `lead_intelligence.py` | AI-powered lead intelligence via xAI Grok micro-prompts with caching | medium |
| `crm_adapters/` | CRM adapter factory + GHL/HubSpot/Salesforce/Pipedrive/Zoho/Insureio/Zapier | directory |

---

## Environment Variables (`.env.example`)

### Flask / Core
- `SECRET_KEY` — Flask session secret
- `DATABASE_URL` — PostgreSQL connection string
- `REDIS_URL` — Redis connection (default: `redis://localhost:6379`)

### GoHighLevel OAuth (CRM)
- `GHL_CLIENT_ID`, `GHL_CLIENT_SECRET` — Public marketplace app credentials
- `GHL_PRIVATE_CLIENT_ID`, `GHL_PRIVATE_CLIENT_SECRET` — Private/internal app credentials
- `GHL_BASE_URL` — API base (default: `https://services.leadconnectorhq.com`)
- `MARKETPLACE_WEBHOOK_SECRET` — Signature verification for GHL webhooks

### AI / xAI Grok
- `XAI_API_KEY` — xAI API key (used for both text and voice Realtime API)

### Twilio
- `TWILIO_MASTER_ACCOUNT_SID`, `TWILIO_MASTER_AUTH_TOKEN` — Master account (manages sub-accounts per user)
- `TWILIO_API_KEY`, `TWILIO_API_SECRET` — For Twilio client tokens
- `TWILIO_TWIML_APP_SID` — TwiML app for voice calls

### Stripe
- `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`
- `STRIPE_WEBHOOK_SECRET` — For validating Stripe webhook events
- `STRIPE_PRICE_ID` — Individual plan price ID
- `STRIPE_AGENCY_STARTER_PRICE_ID`, `STRIPE_AGENCY_PRO_PRICE_ID`
- `AI_MINUTES_PRICE_ID_*` — Usage-based AI minutes packages
- `A2P_REGISTRATION_PRICE_ID` — Stripe price ID for A2P 10DLC registration fee ($19)

### Email
- `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER`
- Supports: SendGrid, Mailgun, Gmail, or custom SMTP

### Google Sheets (legacy backup)
- `GOOGLE_SHEETS_CREDENTIALS` — JSON service account credentials
- `GOOGLE_SHEETS_SPREADSHEET_ID` — Legacy data backup sheet ID

### Discord
- `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` — Discord OAuth app credentials
- `DISCORD_BOT_TOKEN` — Bot token for reading/posting messages
- `DISCORD_REDIRECT_URI` — OAuth callback URL

### Cron / Scheduled Jobs
- `CRON_SECRET` — Shared secret for authenticating cron endpoints (passed as `?key=` query param or `Authorization: Bearer` header)

### Subscription
- `SUBSCRIPTION_PRICE` — Monthly price displayed on checkout (default: 97)

### Cron / Background
- `CRON_SECRET` — Shared secret for authenticating cron endpoints (`/api/cron/send-reminders`, `/api/cron/refresh-tokens`)

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
- `GET|POST /agency-dashboard` — Agency owner dashboard
- `GET|POST /agency-login` — Agency sub-user login
- `POST /api/agency/invite-sub-user` — Invite sub-user
- `POST /api/agency/resend-invite` — Resend invite email
- `POST /api/agency/invite-all` — Invite all pending sub-users
- `GET /api/agency/logs/<location_id>` — Agency member logs

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
- In-memory call tracking: `_active_calls`, `_transfer_requests`, `_call_listeners`
- Supports real-time voice AI conversations using xAI's Realtime API
- A2P 10DLC registration routes for brand/campaign compliance (see A2P 10DLC section below)

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

## AI-Powered Lead Intelligence (lead_intelligence.py)

- **Single micro-prompt**: Gathers all contact context (messages, facts, pipeline, calls, tags, narrative) and sends one prompt to `grok-4-1-fast-non-reasoning`.
- **AI returns JSON**: summary (2-sentence snapshot), temperature (hot/warm/cool/cold + reason), score (0-100), and 2-4 next-best-actions with priority and FontAwesome icon.
- **Smart caching**: Results cached in `contact_intelligence` table (JSONB). Cache invalidated when new messages arrive after the analysis, or after 6 hours. Repeat views cost zero.
- **Cost**: ~$0.001-0.003 per analysis (~200 output tokens).
- **Frontend**: Loading shimmer → temperature pill badge (fire/thermometer/snowflake icons) + score + AI summary + recommended actions panel.
- **Pipeline injection**: `tasks.py` injects pipeline stage into AI system prompt via `get_contact_pipeline_stage()`.
- **Bulk API**: `get_bulk_cached_intelligence(location_id, contact_ids)` returns cached AI data for many contacts in one DB query (zero AI cost). Used by Smart Filters on every dialer load.
- **Batch analysis**: `batch_analyze_contacts(location_id, contact_ids, limit=5)` processes uncached contacts via AI in small batches. Auto-triggered by the dialer when contacts lack cached intelligence.

### AI-Powered Smart Filters (Dialer)

The dialer's Smart Filters use AI intelligence to classify contacts — NOT simple rules like "customer responded = hot." The AI reads full conversation history before classifying.

**Groups** (priority order):
1. **Should Respond** (red) — AI says hot/warm AND lead's last message is unanswered (lead sent a message after the bot's last message)
2. **Hot Leads** (green) — AI temperature = "hot" (actively buying, quoting, strong buying signals)
3. **Warm Leads** (orange) — AI temperature = "warm" (engaged, responding positively)
4. **Cool** (blue) — AI temperature = "cool" (went quiet, slow replies)
5. **Cold** (gray) — AI temperature = "cold" (no engagement, ghosting, not interested)
6. **Do Not Contact** (red) — Opted out via DnD flag or stop keywords
7. **Analyzing...** (spinner) — Contacts pending AI analysis (auto-triggered in background)

**Data flow**:
1. Dialer loads → bulk engagement data fetched (`/voice/contact-engagement`)
2. Bulk AI intelligence fetched from cache (`/voice/contact-intelligence-bulk`) — zero AI cost
3. Contacts grouped by AI temperature into Smart Filter categories
4. Uncached contacts queued for background batch analysis (`/voice/contact-intelligence-analyze`, 5 per request)
5. As each batch completes, contacts move from "Analyzing..." into their correct AI group
6. Subsequent dialer loads are instant from cache

---

## External API (api_v1.py)

- Mounted at `/api/v1/`
- `POST /api/v1/chat/completions` — OpenAI-compatible format
- Bearer token authentication with constant-time comparison (`hmac.compare_digest`)
- Rate limit: `API_RATE_LIMIT_RPM` env var (default 120 req/min) using Redis sliding window
- Per-key usage logged to `api_usage_logs` table

---

## Frontend (Dashboard)

### Template Structure
```
templates/
  dashboard.html              Main dashboard layout
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
    discord_panel.html        Discord message panel (side panel)
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
- **itsdangerous tokens**: Password reset tokens expire in 1 hour
- **Constant-time comparison**: API key authentication uses `hmac.compare_digest`
- **Admin whitelist**: `ADMIN_EMAILS` controls god-mode access
- **Webhook signature verification**: GHL webhooks verified with `MARKETPLACE_WEBHOOK_SECRET`
- **Stripe webhook signature**: Verified with `STRIPE_WEBHOOK_SECRET`
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
gunicorn main:app --threads 40 --timeout 0

# Start workers (in separate terminals)
python worker.py production
python worker.py demo
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
- `lead_intelligence.py` fires one AI micro-prompt per contact, caches results in `contact_intelligence` table; cache invalidated by new messages or 6-hour TTL. Bulk cache reads power Smart Filters; batch analysis auto-runs for uncached contacts
- SMS routing: `tasks.py` checks `sms_send_via` column — `'ghl'` routes through GHL API, `'+1...'` routes through `twilio_sms.py` direct sender
- Dialer uses iPhone-style app paradigm: `iosOpenApp()` / `iosGoHome()` for Messages, Calls, Voicemail, and Inbox apps
- `_loadContactIntelligence(contactId)` async-loads AI intelligence into `#igb-ai-summary`, `#igb-nba-section`, `#igb-pipeline-badge` placeholders in contact detail panel; also updates `_igbIntelCache` so Smart Filter groups re-render in real time

---

## Carrier List

`carrier_list.py` contains 63 insurance carriers as `{"key": "...", "name": "..."}` dicts for the dashboard chip picker UI. `insurance_companies.py` has 270+ carrier names including aliases for AI-powered carrier detection in conversations. The bot only references carriers the agent has selected.

---

## Subscription Tiers

- **Individual Plan**: Single agency user, standard features
- **Agency Starter**: Agency owner + limited sub-users
- **Agency Pro**: Agency owner + more sub-users + advanced features
- **AI Minutes**: Add-on usage-based billing for AI voice processing

Subscriptions managed via Stripe. Users without active subscriptions see a paywall on the dashboard.
