# CLAUDE.md — InsuranceGrokBot Flask-Webhook App

## What This App Is

**InsuranceGrokBot** is a white-label AI-powered SMS and voice bot platform specifically built for insurance agents. It connects to CRMs (GoHighLevel primary, HubSpot supported) via OAuth, intercepts incoming webhook events (new leads, SMS messages, etc.), and uses xAI's Grok LLM to generate intelligent, context-aware replies — automatically sent back through Twilio as white-label SMS (users never see "Twilio" branding).

The system is multi-tenant SaaS: each subscribing insurance agency gets their own isolated bot instance with their own phone numbers, carrier list, prompt configuration, and conversation history. It also supports agency owners managing multiple sub-accounts.

**Twilio ISV Architecture**: This app operates as a **Twilio ISV (Independent Software Vendor) using sub-accounts**. One master Twilio account owns the platform; each subscribing user gets their own Twilio sub-account. All Twilio Trust Hub, A2P 10DLC, Voice Integrity, SHAKEN/STIR, and CNAM operations follow the ISV/sub-account model — never the direct customer model.

---

## Architecture Overview

```
Browser / GHL Webhook
        │
        ▼
Gunicorn (40 threads) ──► Flask app (main.py → 16 blueprints + voice/ package)
        │
        ├── PostgreSQL (psycopg2 threaded pool, 2–20 connections, 30 tables)
        ├── Redis + RQ (background job queues)
        ├── Twilio (SMS send/receive, Voice, sub-accounts)
        ├── xAI Grok API (LLM for text replies + Realtime API for voice)
        ├── Stripe (subscriptions + AI Minutes usage billing)
        ├── GoHighLevel OAuth (CRM data access — primary)
        ├── HubSpot OAuth (CRM data access — supported)
        ├── Discord (embedded team chat in dashboard)
        └── Slack (embedded workspace chat in dashboard)
```

### Worker Architecture

```
Deployment (Render / Railway):
  Flask-Webhook:  gunicorn main:app --worker-class gthread --threads 40 --timeout 14400
  worker:         python worker.py --workers=2 production intelligence   (webhooks + AI intelligence, 2 processes)
  worker-bg:      python worker.py website demo      (GHL sync, backfill, demo chat)
  Redis:          managed Redis instance
```

### RQ Queues

| Queue | Worker | Purpose | Tasks |
|-------|--------|---------|-------|
| `production` | `worker` | Fast webhook processing | `process_webhook_task` (120s) |
| `intelligence` | `worker` | AI lead intelligence (separate from webhooks) | `analyze_contact_intelligence_task` (30s), `analyze_contacts_batch_task` (300s) |
| `website` | `worker-bg` | Long-running GHL sync + recovery | `run_incremental_sync_all` (30m), `deep_sync_conversations` (2h), `backfill_failed_webhooks` (10m) |
| `demo` | `worker-bg` | Demo chat isolation | `process_webhook_task` for demo contacts (120s) |

All queues are defined in `extensions.py` and initialized via `ensure_redis()`. Worker startup: `python worker.py <queue1> [queue2] ...` (defaults to `production` if no args). Use `--workers=N` to fork multiple worker processes on the same server.

### Twilio Dual Architecture: Direct Customer + ISV Sub-Accounts

This platform operates two distinct Twilio patterns simultaneously:

1. **Master Account (platform owner) = Direct Customer**
   - The platform owner's own Twilio account
   - Uses the Primary Business Profile directly (already approved in Twilio Console)
   - No Secondary profiles needed — uses Primary for CNAM, Voice Integrity, A2P
   - Detected via `is_master_account(sub_account_sid)` (compares to `TWILIO_ACCOUNT_SID`)

2. **Subscriber Sub-Accounts = ISV/Reseller with Sub-Accounts**
   - Each subscribing agency gets their own Twilio sub-account
   - Sub-accounts are fully independent: own Account SID + Auth Token
   - Must create Secondary Customer Profile linked to master's Primary
   - All Trust Hub operations use sub-account's own credentials

```
Master Twilio Account (platform owner — DIRECT CUSTOMER)
├── Primary Business Profile (created once in Console, twilio-approved)
├── Own CNAM Trust Product → linked to Primary Profile
├── Own Voice Integrity Trust Product → linked to Primary Profile
│
├── Sub-Account: Agency A (ISV CUSTOMER)
│   ├── Own Account SID + Auth Token (fully independent credentials)
│   ├── Secondary Customer Profile → linked to Primary via EntityAssignment
│   ├── Phone Numbers (purchased on sub-account)
│   ├── CNAM Trust Product → linked to Secondary Profile
│   ├── Voice Integrity Trust Product → linked to Secondary Profile
│   ├── A2P Brand + Campaign (registered under sub-account)
│   └── TwiML App (webhook routing)
│
├── Sub-Account: Agency B (ISV CUSTOMER)
│   └── (same structure — fully independent from Agency A)
└── ...
```

**Sub-Account Independence Rules:**
- **Authentication**: TrustHub and Messaging APIs MUST use `get_sub_account_client_native(sub_account_sid, sub_account_auth_token)` — the sub-account's OWN SID + auth token. Using master credentials returns master-account resources, causing cross-account contamination.
- **Every sub-account is independent**: Each has its own Customer Profile, Trust Products, EndUsers, Phone Numbers. Nothing is shared between sub-accounts except the linkage back to the master's Primary Business Profile.
- **Primary Business Profile**: Created ONCE on the master account via the Twilio Console. All sub-account Secondary Profiles link back to this via EntityAssignment. SID discovered at runtime via `_find_primary_profile_sid()`.
- **Secondary Customer Profiles**: Created on each sub-account using `get_sub_account_client_native()` with `policy_sid=RNdfbf3fae0e1107f8aded0e7cead80bf5`. Must be linked to the Primary Profile via EntityAssignment. Reused across A2P, Voice Integrity, SHAKEN/STIR, and CNAM on that sub-account.
- **Trust Products** (CNAM, Voice Integrity, A2P): Created on the sub-account using sub-account credentials, linked to the Secondary Profile via EntityAssignment.
- **Phone Numbers**: Purchased and managed on each sub-account, assigned to both the Secondary Profile and any Trust Products.

**All Twilio SDK calls for sub-accounts use `get_sub_account_client_native()`.** The SDK handles evaluation, entity creation, assignments. The only exception is status transitions (`pending-review`) which use `_trusthub_update_status()` (direct HTTP POST) to work around a known Twilio SDK bug where `.update(status=...)` silently drops the Status parameter.

**Key Policy SIDs** (static across all Twilio accounts — never change these):

| Policy | SID | Used For |
|--------|-----|----------|
| Secondary Customer Profile | `RNdfbf3fae0e1107f8aded0e7cead80bf5` | `customer_profiles.create()` on sub-accounts |
| CNAM Trust Product | `RNf3db3cd1fe25fcfd3c3ded065c8fea53` | `trust_products.create()` for CNAM |
| Voice Integrity Trust Product | `RN5b3660f9598883b1df4e77f77acefba0` | `trust_products.create()` for Voice Integrity |
| A2P 10DLC Brand | `RNb0d4771c2c98518d916a3d4cd70a8f8b` | `trust_products.create()` for A2P brand registration |

**Twilio documentation references** (always use the correct guide for the account type):

*Direct Customer (master account):*
- Voice Integrity Direct: https://www.twilio.com/docs/voice/spam-monitoring-with-voiceintegrity/voice-integrity-onboarding/voice-integrity-trust-hub-api-direct-customer
- CNAM: https://www.twilio.com/docs/voice/brand-your-calls-using-cnam

*ISV/Reseller with Sub-Accounts (subscriber accounts):*
- Voice Integrity ISV/Subaccounts: https://www.twilio.com/docs/voice/spam-monitoring-with-voiceintegrity/voice-integrity-onboarding/voice-integrity-trust-hub-api-isvs-subaccounts
- Secondary Customer Profile API: https://www.twilio.com/docs/trust-hub/trusthub-rest-api/api-create-secondary-customer-profile
- A2P 10DLC ISV Onboarding: https://www.twilio.com/docs/messaging/compliance/a2p-10dlc/onboarding-isv-api
- SHAKEN/STIR ISV/Subaccounts: https://www.twilio.com/docs/voice/trusted-calling-with-shakenstir/shakenstir-onboarding/shaken-stir-trust-hub-api-isvs-subaccounts

---

## Core Files

| File | Purpose | Size |
|------|---------|------|
| `main.py` | Flask app factory — wires blueprints, Redis, DB, Flask-Login/Mail, voice bridge | ~323 lines |
| `db.py` | PostgreSQL layer — connection pool, all data access functions, schema (30 tables) | ~4200 lines |
| `tasks.py` | Background job engine — AI pipeline, webhook processing | ~1400 lines |
| `voice_bridge.py` | Backward-compatibility shim — re-exports from `voice/` package | shim |
| `extensions.py` | Shared Redis/RQ initialization, queue definitions, `ensure_redis()` | small |
| `worker.py` | RQ worker startup script | small |
| `api_v1.py` | External API blueprint (`/api/v1/`) — OpenAI-compatible + Training API | small |
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
| `email_templates.py` | Premium HTML email builder for invites, onboarding, reminders | ~1020 lines |
| `booking_detection.py` | Calendar booking detection via xAI — detects when contacts want to schedule | ~560 lines |
| `translations.py` | i18n/internationalization support — multi-language UI strings | small |
| `token_encryption.py` | Fernet symmetric encryption/decryption for OAuth tokens at rest | small |
| `lead_resolver.py` | Smart lead type detection combining GHL tags + dates | small |
| `message_utils.py` | Message batching and rapid-fire message collection utilities | small |
| `payload_utils.py` | Webhook payload normalization for flexible GHL field extraction | small |
| `forms.py` | Flask-WTF form definitions | small |
| `workflow_engine.py` | Workflow execution engine — trigger matching, step handlers, condition evaluation, cron-based triggers | ~2300 lines |
| `voice/predictive_engine.py` | Erlang-C pacing, TCPA compliance tracker, agent state machine, callback queue, timezone/consent lookup | medium |
| `voice/stream.py` | Call listen/intercept — live audio streaming WebSocket + takeover endpoint | small |
| `crm_adapters/` | CRM adapter factory + GHL/HubSpot/Salesforce/Pipedrive/Zoho/Insureio/Zapier | directory |
| `crm_providers/` | CRM Provider plugin system (ABC base + GHL wrapper + HubSpot full integration) | directory |
| `crm_providers/base.py` | `CRMProvider` ABC — interface for all CRM integrations | small |
| `crm_providers/ghl/__init__.py` | GHL provider — wraps existing GHL code into CRMProvider interface | small |
| `crm_providers/hubspot/__init__.py` | HubSpot provider orchestrator — wires OAuth, sync, webhooks, logging, resolver | medium |
| `crm_providers/hubspot/oauth.py` | HubSpot OAuth2 flow (initiate, callback, token refresh) | medium |
| `crm_providers/hubspot/inbound.py` | HubSpot webhook handler (batched events, HMAC-SHA256 v3 verification) | medium |
| `crm_providers/hubspot/sync.py` | HubSpot data sync engine (conversations, deals, contacts → local Postgres) | large |
| `crm_providers/hubspot/logger.py` | HubSpot activity logging (SMS → Communication, calls → Call, notes → Note) | medium |
| `crm_providers/hubspot/resolver.py` | HubSpot contact search via CRM v3 Search API | small |
| `crm_providers/hubspot/crm_card.py` | HubSpot CRM Card endpoint (AI intelligence, zero cost, cache-only) | medium |

### Blueprints (`blueprints/`)

All HTTP routes have been extracted from `main.py` into dedicated Flask blueprints:

| File | Purpose | Size |
|------|---------|------|
| `auth.py` | Login, register, password reset, claim-account flows | ~416 lines |
| `public.py` | Marketing pages — home, comparison, FAQ, contact, privacy, terms | ~226 lines |
| `webhooks.py` | GHL webhook receiver, Conversation Provider outbound handler | ~654 lines |
| `oauth.py` | GHL OAuth initiate/callback, token refresh | ~1137 lines |
| `dashboard.py` | Main dashboard, config save, bot settings, voice config, onboarding | ~1038 lines |
| `billing.py` | Stripe webhooks, checkout, plan switching, AI minutes, portal | ~1186 lines |
| `admin.py` | God Mode admin dashboard, impersonation, marketplace installs | ~747 lines |
| `agency.py` | Agency dashboard, sub-user invites, KPIs, agent stats, call log | ~934 lines |
| `demo.py` | Demo chat endpoints, session init/reset, transcript download | ~540 lines |
| `discord.py` | Discord OAuth, servers, channels, messages | ~444 lines |
| `slack.py` | Slack OAuth, workspace management, channel messages | ~446 lines |
| `cron.py` | Scheduled jobs — token refresh, reminders, webhook recovery, GHL sync | ~261 lines |
| `inbox.py` | Unified inbox — conversations list, contact threads, SSE notifications | ~523 lines |
| `calendar.py` | GHL calendar fetching and booking | ~554 lines |
| `google_calendar.py` | Google Calendar OAuth and integration | ~369 lines |
| `team.py` | Team management — invite members, roles, permissions, audit log | ~1233 lines |
| `embed.py` | Embeddable panel routes — CRM iframe embedding (intelligence, dialer, panel) | ~122 lines |
| `workflows.py` | Workflow CRUD, AI builder, pre-built template seeding, test runs | ~1200 lines |

### Voice Package (`voice/`)

The voice bridge has been decomposed from a single `voice_bridge.py` (~193KB) into a modular package:

| File | Purpose | Size |
|------|---------|------|
| `numbers.py` | Phone number management, search, buy, release, Trust Hub, spam protection, number health | ~2513 lines |
| `dialer.py` | Multi-line power dialer, predictive dialing, batch status polling | ~1836 lines |
| `predictive_engine.py` | Erlang-C pacing, TCPA compliance tracker, agent state machine, callback queue | ~851 lines |
| `contacts.py` | Contact data fetching, engagement metrics, bulk operations | ~828 lines |
| `stream.py` | Twilio ↔ xAI Realtime WebSocket bridge, audio streaming | ~825 lines |
| `call_history.py` | Call history CRUD, recording management | ~731 lines |
| `voice_prompt.py` | Voice AI system prompt builder | ~695 lines |
| `a2p.py` | A2P 10DLC brand/campaign registration, import, status polling | ~612 lines |
| `outbound.py` | Outbound call initiation, TwiML generation | ~452 lines |
| `intelligence.py` | AI contact intelligence routes — bulk cache, batch analysis | ~408 lines |
| `twiml_routes.py` | TwiML webhook handlers — call status, gather, transfer complete | ~391 lines |
| `stats.py` | Call statistics, KPIs, duration buckets, daily/hourly breakdown | ~312 lines |
| `recordings.py` | Recording playback, transcription, Twilio proxy | ~243 lines |
| `setup.py` | Voice blueprint setup, Twilio client initialization | ~209 lines |
| `audio.py` | Audio pipeline — soxr resampling, mulaw/PCM conversion, EQ | ~200 lines |
| `helpers.py` | Shared voice utilities | ~180 lines |
| `voice_tools.py` | Voice AI tool definitions for function calling | ~178 lines |
| `insights.py` | Call insights and analytics | ~140 lines |
| `redis_state.py` | Redis-backed call state (`active_calls`, `transfer_requests`, `overflow_alerts`) with sync + async APIs | ~245 lines |
| `call_state.py` | Call state re-exports from `redis_state.py`, TwiML helpers, in-process `call_listeners` dict | ~103 lines |
| `call_history_helpers.py` | Call history helper functions | small |

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
- `STRIPE_PREDICTIVE_DIALER_PRICE_ID` — Predictive Dialer plan price ID ($349.98/mo)
- `AI_MINUTES_PRICE_ID_500`, `AI_MINUTES_PRICE_ID_2000`, `AI_MINUTES_PRICE_ID_5000`, `AI_MINUTES_PRICE_ID_10000` — Usage-based AI minutes packages

### Email
- `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER`, `MAIL_USE_TLS`, `MAIL_USE_SSL`
- `MAILGUN_API_KEY`, `MAILGUN_DOMAIN` — For API-based sending via `send_email_api.py`

### Google Sheets (subscriber sync)
- `GOOGLE_CREDENTIALS` — JSON service account credentials
- `SUBSCRIBER_SHEET_URL` — CSV export URL for subscriber sync
- `SUBSCRIBER_SHEET_EDIT_URL` — Spreadsheet edit URL

### HubSpot CRM
- `HUBSPOT_CLIENT_ID` — HubSpot public app client ID (from HubSpot Developer Portal)
- `HUBSPOT_CLIENT_SECRET` — HubSpot app client secret (used for OAuth token exchange + webhook HMAC-SHA256 v3 signature verification)

### Google Calendar (optional)
- `GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_CALENDAR_CLIENT_SECRET`, `GOOGLE_CALENDAR_REDIRECT_URI`

### Discord
- `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` — Discord OAuth app credentials
- `DISCORD_BOT_TOKEN` — Bot token for reading/posting messages
- `DISCORD_REDIRECT_URI` — OAuth callback URL

### Slack (optional)
- `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_REDIRECT_URI`

### GHL Conversation Providers
- `GHL_SMS_CONVERSATION_PROVIDER_ID` — Custom SMS provider ID (default: `699c84aef36d66cc10a56e82`)
- `GHL_CALL_CONVERSATION_PROVIDER_ID` — Call provider ID (default: `699c83535fc465bbff87a78d`)

### Stripe (additional)
- `STRIPE_SEAT_PRICE_ID` — Per-seat add-on pricing for team members

### Affiliate
- `REWARDFUL_API_SECRET` — Rewardful affiliate/referral tracking

### Cron / Scheduled Jobs
- `CRON_SECRET` — Shared secret for authenticating cron endpoints (passed as `?key=` query param or `Authorization: Bearer` header)

### Railway Build Config (not used in app code)
- `NIXPACKS_PYTHON_VERSION` — Python version for Railway builds

---

## Database Schema (30 tables)

All tables created in `db.py`'s `init_db()` function (plus `contact_intelligence` in `lead_intelligence.py`):

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
| `failed_webhook_payloads` | Stores failed webhook payloads for retry/recovery |
| `app_settings` | Global application settings (key-value) |
| `contact_cache` | Cached GHL contact data for fast local lookups |
| `ghl_conversations` | Synced GHL message history (contact_id, message_type, direction, body, source, ghl_message_id UNIQUE) |
| `ghl_opportunities` | Synced GHL pipeline/deal data (pipeline_name, stage_name, status, monetary_value, ghl_opportunity_id UNIQUE) |
| `ghl_sync_state` | Sync progress tracking (resource_type, last_sync_at, last_cursor, sync_status, total_synced) |
| `number_health` | Phone number health tracking — spam scores, carrier status, rotation state |
| `slack_connections` | Slack OAuth tokens per user |
| `slack_workspaces` | Saved Slack workspaces per user |
| `uninstall_feedback` | GHL marketplace uninstall feedback/reasons |
| `location_users` | Team members per location — roles, permissions, invited/claimed status |
| `team_audit_log` | Audit trail for team management actions (invites, role changes, removals) |
| `contact_intelligence` | AI intelligence cache (contact_id PK, analysis JSONB, analyzed_at — in `lead_intelligence.py`) |

### Database Connection Pool
- `ThreadedConnectionPool` with semaphore queuing
- `DB_POOL_MIN=2`, `DB_POOL_MAX=20`, `DB_POOL_WAITERS=500`, `DB_POOL_TIMEOUT=10s`
- All DB calls use `get_db_connection()` / `return_db_connection()` — always return connections in `finally` blocks
- Google Sheets legacy backup connection attempted at startup (non-fatal if fails)

---

## HTTP Routes (blueprints/)

### Public / Marketing (`blueprints/public.py`)
- `GET /` — Home page
- `GET /for-agencies` — Agency landing page
- `GET /for-individuals` — Individual agent landing page
- `GET /comparison`, `/comparison/text-drip`, `/comparison/dialers` — Comparison pages
- `GET /dialer` — Dialer marketing page
- `GET /sms` — SMS marketing page
- `GET /workflows` — Workflow automation marketing page
- `GET /getting-started` — Setup guide
- `GET /a2p-guide` — A2P 10DLC registration guide
- `GET /articles` — Articles/blog listing page (7 articles)
- `GET /articles/top-tools-life-insurance-agents` — SEO article: Top Tools for Life Insurance Agents
- `GET /articles/ai-life-insurance-good-thing` — SEO article: Why AI in Life Insurance Is a Good Thing
- `GET /articles/gohighlevel-vs-hubspot-insurance` — SEO article: GoHighLevel vs HubSpot for Insurance
- `GET /articles/speed-to-lead-insurance` — SEO article: Speed to Lead in Insurance Sales
- `GET /articles/spam-likely-killing-sales` — SEO article: How Spam Likely Is Killing Your Sales
- `GET /connect` — Connect With Us / social media page (Facebook, LinkedIn)
- `GET /about` — About page
- `GET /affiliate` — Affiliate program page
- `GET /reviews` — Reviews page (POST submits review)
- `GET /contact`, `/privacy`, `/terms`, `/disclaimers`
- `GET /faq`, `/integrations`, `/support`, `/setup-guide`
- `GET /spam-protection` — Spam monitoring & number health marketing page
- `GET|POST /uninstall-feedback` — GHL marketplace uninstall feedback form
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
- `GET /oauth/callback` — GHL OAuth callback (exchanges code for tokens, stores in DB). Agency detection: if OAuth response has `companyId` but no `locationId`/users → agency owner (saved to `agency_billing` with `company_id`). Individual agents with matching `companyId` auto-link to agency.
- `GET /oauth/loading` — Loading page while OAuth completes
- `GET /refresh` — Manually refresh GHL OAuth tokens
- `GET /app` — App landing page post-install

### GHL Webhooks
- `POST /webhook` — Main GHL webhook receiver (queues jobs via RQ)
- `POST /webhook/app-installed` — GHL marketplace app install webhook

### Stripe
- `POST /stripe-webhook` — Stripe webhook handler (subscription lifecycle)
- `GET /checkout` — Individual plan checkout (used by all users including agency owners)
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
- `GET|POST /agency-login` — Agency owner login (queries `agency_billing` table, role must be `agency_owner`)
- `POST /api/agency/invite-sub-user` — Invite sub-user
- `POST /api/agency/resend-invite` — Resend invite email
- `POST /api/agency/invite-all` — Invite all pending sub-users
- `GET /api/agency/logs/<location_id>` — Agency member logs
- `GET /api/agency/kpis?period=<today|week|month|all>` — Aggregated KPIs across all sub-accounts (calls, connected, rate, duration, messages, active agents, daily/hourly charts, prior period comparison)
- `GET /api/agency/dashboard-stats?period=<today|week|month|all>` — Comprehensive agency dashboard stats with insurance-specific KPIs (connect rate, duration buckets 45s/2m/5m/10m, avg daily dials per agent, speed to lead, top 5 leaderboards by connect rate/calls/duration, daily/hourly charts, prior period comparison)
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

### HubSpot CRM Integration
- `GET /hubspot/oauth/initiate` — Start HubSpot OAuth flow (redirects to HubSpot consent screen)
- `GET /hubspot/oauth/callback` — HubSpot OAuth callback (exchanges code for tokens, stores in `subscribers.crm_config`)
- `POST /hubspot/webhook` — HubSpot inbound webhook receiver (batched events, HMAC-SHA256 v3 verified)
- `GET /hubspot/crm-card` — CRM Card data fetch URL (returns AI intelligence JSON for HubSpot sidebar)
- `GET /hubspot/crm-card/health` — CRM Card health check

### Embeddable Panels (`blueprints/embed.py`)
- `GET /embed/panel` — Mini-dashboard without sidebar/topbar (for CRM iframe embedding)
- `GET /embed/dialer` — Dialer focused on one contact (for CRM iframe embedding)
- `GET /embed/intelligence/<contact_id>` — AI intelligence card only (for HubSpot CRM Card iframes)
- `GET /embed/intelligence/<contact_id>/json` — JSON API for intelligence data

### Website Bot
- `POST /website-bot-webhook` — External website chatbot webhook

### Team Management (`blueprints/team.py`)
- `GET /api/team/ghl-users` — Fetch GHL location users for invite picker
- `GET /api/team/members` — List all team members with roles and status
- `POST /api/team/invite` — Invite team member (creates `location_users` record, sends email)
- `POST /api/team/resend-invite` — Resend invite email to pending member
- `GET|POST /claim-seat` — Team member claim flow (set password, activate account)
- `POST /api/team/permissions` — Update member permissions (JSON array)
- `POST /api/team/role` — Change member role (admin/agent/viewer)
- `POST /api/team/toggle-active` — Enable/disable team member
- `POST /api/team/activate-voice` — Provision voice (Twilio sub-account) for team member
- `GET /api/team/agent-kpis` — Per-agent KPIs (calls, connected, duration, messages)
- `GET /api/team/stats` — Aggregated team statistics
- `GET /api/team/audit-log` — Team management audit trail
- `GET /api/team/onboarding-status` — Per-member onboarding completion check
- `POST /api/team/remove` — Remove team member
- `POST /api/team/checkout` — Stripe checkout for additional team seats
- `GET /api/team/billing-info` — Team billing information
- `GET /api/team/roles` — Available roles and their permissions

### Slack (`blueprints/slack.py`)
- `GET /slack/connect` — Start Slack OAuth
- `GET /slack/callback` — Slack OAuth callback
- `GET /slack/disconnect` — Disconnect Slack
- `GET /api/slack/status` — Slack connection status
- `GET /api/slack/workspaces` — User's Slack workspaces
- `GET /api/slack/channels/<workspace_id>` — List workspace channels
- `GET|POST /api/slack/messages/<channel_id>` — Read/send messages

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

### Call Listen & Intercept (Blueprint, voice/stream.py)
- `WS /voice/listen-stream` — WebSocket endpoint for live call audio monitoring. Streams 8kHz mulaw audio from Twilio media stream to the agent's browser in real time. Pushes `None` sentinel to listener queues when call ends for instant cleanup.
- `POST /voice/takeover` — Intercept an active AI call. Pre-validates call status via Twilio before attempting redirect. Returns structured errors (e.g. "call already ended") instead of Twilio 400 explosions.

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
- `GET /voice/ai-minutes-check` — Check AI minutes balance + warning level for overflow (ok/low/critical/empty)
- `GET /voice/overflow-alerts` — Pending overflow transfer alerts for current location (hot leads wanting transfer)
- `POST /voice/overflow-alerts/dismiss` — Dismiss an overflow alert (agent chose "Let AI Book")
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

## Pre-Built Workflow Templates (blueprints/workflows.py)

Every new location gets **4 pre-built workflow templates** automatically seeded on first load (all in `draft` status — users activate when ready). Seeding is lazy: `_seed_default_workflows()` runs inside `GET /api/workflows` and checks `SELECT 1 FROM workflows WHERE location_id = %s LIMIT 1` before inserting.

| Workflow | Trigger | Description |
|----------|---------|-------------|
| **Speed to Lead** | `contact_created` | 30s wait → personalized intro SMS → 5min wait → if no reply → AI call → tag `speed-to-lead-contacted` |
| **Aged Lead Re-engagement** | `lead_age` (30 days) | Check temperature → cold gets AI low-pressure check-in, warm gets AI re-engagement → tag `re-engage-sent` → 2-day wait → AI follow-up or tag `re-engaged` |
| **SMS Response Handler** | `sms_received` | Check DNC tag → if hot lead → tag + AI call; if warm/cool → AI-drafted reply |
| **Re-engage Cold Leads** | `no_response` (7 days) | Gentle check-in SMS → tag `re-engage-attempt` → 1-day wait → if replied tag `re-engaged`, else AI call → 3-day wait → final SMS → tag `nurture-complete` |

Templates use merge fields (`{{firstName}}`, `{{operatorName}}`, `{{companyName}}`), AI intelligence conditions (`temperature_is`, `responded_within`), and `exit_on_reply` for auto-exit when the contact responds.

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

### SMS Bot Conversation Intelligence

The SMS bot uses a multi-layered AI pipeline to maintain human-like conversations. Key systems:

**Conversation Stages (`conversation_engine.py`)**:
- 7 stages: `INITIAL_OUTREACH` → `RAPPORT` → `QUALIFYING` → `OBJECTION_HANDLING` → `BOOKING` → `BOOKED` → `FOLLOW_UP`
- `RAPPORT` stage (max 2 consecutive turns) — trust-building between qualifying turns. Detects when lead sends messages with zero qualifying substance (no coverage, goals, objections). Turn 1: match energy, find common ground. Turn 2: bridge naturally to qualifying. Turn 3+: force back to QUALIFYING
- Stage detection uses `_count_consecutive_rapport_turns()` with word-boundary-safe regex matching to prevent false positives (e.g., "needlessly" won't match "need")
- Qualifying signals split into `_QUALIFYING_PHRASES` (substring) and `_QUALIFYING_WORDS` (word-boundary regex)

**Conversational Coherence (`prompt.py`)**:
- "RESPOND TO WHAT THEY ACTUALLY SAID" rule — LLM must read message in context of its previous message
- Prevents forcing insurance pivots when lead is discussing unrelated topics
- If they say "you text too much", acknowledge THAT — do not ignore and ask about insurance
- `CONVERSATIONAL_THREAD` section added to narrative output tracks what lead is currently talking about

**Conversation Context Depth**:
- LLM receives last 20 messages (up from 8) in multi-turn `user`/`assistant` format
- Narrative observer processes last 30 messages (up from 14) for intelligence gathering
- Multi-turn message format enables proper conversation threading vs. flat text

### Objection Detection & Handling (Closer's Mindset)

**Philosophy: Nothing slips through.** ONLY non-objections: answering questions, asking questions, expressing interest, agreeing, providing info. EVERYTHING else is an objection and an opportunity. Better to classify a neutral message as objection than to miss a real one.

**Six Objection Types with 290+ keyword phrases across all types:**

| Type | Example Phrases | Count |
|------|----------------|-------|
| `NOT_INTERESTED` | "no thanks", "hard pass", "leave me alone", "whatever", "kick rocks" | ~60 |
| `SPOUSE_PARTNER` | "ask my wife", "check with my partner", "my accountant", "need approval" | ~50 |
| `PRICE_MONEY` | "too expensive", "fixed income", "waste of money", "anything cheaper" | ~40 |
| `ALREADY_COVERED` | "have insurance", "employer covers", "already have an agent", "just renewed" | ~40 |
| `THINK_ABOUT_IT` | "sleep on it", "not ready", "bigger fish to fry", "rain check" | ~50 |
| `BUSY_TIMING` | "in a meeting", "swamped", "driving", "hit me up later" | ~50 |

Plus catch-all regex for short dismissive messages: `^no+$`, `^nah+$`, "lol no", "why would i", etc.

**Two-Phase Objection Framework ("Logistical First. Fear Second.")**:
- **Phase 1 (default)**: Solve the practical side. Ask logistical questions. Never immediately re-pitch.
- **Phase 2 (triggers at 2+ same objection)**: Address the fear underneath. Make inaction feel concrete.
- Per-objection-type keyword matching with `_OBJECTION_LOG_KEYWORDS` dict (6-8 synonyms per type) for robust phase detection

**Objection Phase Detection** (`sales_director.py`):
- Walks objection log entries and matches against keyword lists per type
- Handles LLM narrative variations (e.g., "too expensive" matches PRICE_MONEY even if log says "cost")

### TCPA Compliance (Stop Word Handling)

**Stripped to legal minimum — sales objections are NOT opt-outs:**

| Category | Words | Action |
|----------|-------|--------|
| `TCPA_STOP_EXACT` | "stop", "unsubscribe", "cancel" (bare, ≤3 words) | Silently stop messaging |
| `TCPA_STOP_PHRASES` | "remove me", "opt out", "do not call/text/contact/message" | Silently stop messaging |
| `TCPA_EXCEPTIONS` | "stop texting", "stop calling", "stop messaging" | NOT TCPA — treated as NOT_INTERESTED objection |

**Removed from TCPA**: "not interested", "leave me alone", "lose my number", "delete my number", "blocked", "quit", "end". All now classified as sales objections routed to objection handling.

### Human Identity Protocol (CRITICAL — applies to SMS and Voice)

The bot must **never** reveal that it is a bot, AI, virtual assistant, or automated system. The lead must always believe they are texting or speaking with a real human insurance advisor. Enforced at three layers:

**Layer 1 — System Prompt (`prompt.py`)**:
- "YOU ARE A REAL PERSON" section with explicit forbidden words/phrases
- Forbidden: "system", "technical issue", "AI", "bot", "virtual assistant", "automated", "calendar is unavailable", "I'll send you a confirmation", "the system will"
- Guidance: handle problems the way a real person would — "ah that one just got grabbed" not "there was an error accessing my calendar"

**Layer 2 — Context Nudges (`tasks.py`)**:
- All booking confirmation/failure nudges use human-sounding framing
- Success: "You just got them on the calendar for [time]. Ask if they see the invite."
- Slot taken: "That one just got grabbed — offer alternatives."
- Calendar down: "You don't have your schedule pulled up right now."
- Never: "system COULD NOT book", "technical issue", "temporarily unavailable"

**Layer 3 — Reply Sanitizer (`reply_sanitizer.py`, last line of defense)**:
- 16+ contamination markers block bot/AI identity reveals even if LLM generates them
- Blocked phrases: "i'm an ai", "i am a bot", "virtual assistant", "automated message", "technical issue", "system error", "calendar is unavailable", "artificial intelligence", "language model"
- Unicode emoji stripping — strips all emojis from LLM output, blocks emoji-only replies
- If any contamination marker is detected, the message is blocked before sending

**Voice Tools (`voice/voice_tools.py`)**:
- Calendar check failed: "You don't have your schedule pulled up right now" (not "Calendar is temporarily unavailable")
- Booking failed: "That time is not available" (not "Booking failed due to a technical issue")
- Booking success: "Ask if they got the invite in their email" (not "they'll receive a confirmation")
- Transfer failed: "Senior advisor is not available right now" (not "I wasn't able to initiate the transfer")

**Booking Confirmation Language**:
- After booking: "Got you down for [time]. Do you see the invite?" — GHL automatically sends the calendar invite via email
- Never say: "I'll send you a confirmation" or "a calendar invite is coming" — it's already sent
- Never say: "let me send you the confirmation" — we don't have email capabilities; GHL handles it

### Booking Detection Precision (`conversation_engine.py`)

- Booking keywords refined to prevent false positives — generic "works for me" replaced with specific "that time works", "those times work"
- "I'm ready" replaced with "I'm ready to get started" (prevents matching casual affirmation)

### Articulated Impact Detection (`conversation_engine.py`)

40+ keywords detecting when a lead expresses emotional weight about coverage:
- Emotional weight: "keep me up", "can't sleep", "burden", "devastating", "terrif", "afraid"
- Consequences: "what happens to", "who takes care", "wife would", "they'd be"
- Personal resolve: "gotta make sure", "need to protect", "can't leave them", "owe it to"
- Gap awareness: "not enough", "won't cover", "no safety net"

### Lead Intelligence Scoring Updates (`lead_intelligence.py`)

- "Not interested" no longer forces cold/score 5 — now scored as cool/15-30 (objection, not dead)
- TCPA opt-out words still = cold/score 5
- `should_respond=true` for sales objections (bot should keep trying)
- Dialer Smart Filters updated: "not interested" contacts stay in Cool group, not Do Not Contact

### SMS Bot Subscription Tier

- **SMS Bot**: $99.98/mo — AI texting only, no dialer/voice features. `subscription_tier = 'sms_bot'`
- Gated from 85+ voice routes via `before_request` hook on `voice_bp`
- Forces `sms_send_via='ghl'` (no Twilio direct sending)
- Dashboard hides Dialer, Voice Config, AI Minutes tabs for sms_bot users

---

## Voice Package (`voice/`)

- **Flask Blueprint** `voice_bp` mounted in main.py (decomposed from monolithic `voice_bridge.py`)
- `voice_bridge.py` is now a backward-compatibility shim that re-exports from `voice/`
- Bidirectional WebSocket bridge: Twilio mulaw 8kHz ↔ xAI PCM 16kHz (`voice/stream.py`)
- Audio pipeline: soxr resampling + mulaw/PCM conversion + scipy Butterworth EQ (`voice/audio.py`)
- Redis-backed call state: `active_calls`, `transfer_requests`, `overflow_transfer_alerts` via `voice/redis_state.py` (sync + async APIs)
- `voice/call_state.py` re-exports Redis functions for backward compatibility; `call_listeners` stays in-process (WebSocket-to-WebSocket audio relay)
- Outbound call initiation and TwiML generation (`voice/outbound.py`)
- Call history CRUD and recording management (`voice/call_history.py`)
- Voice AI system prompt builder (`voice/voice_prompt.py`)
- Voice AI tool definitions for function calling (`voice/voice_tools.py`)
- TwiML webhook handlers — status callbacks, gather, transfer complete (`voice/twiml_routes.py`)
- Call statistics and analytics (`voice/stats.py`, `voice/insights.py`)
- Recording playback, transcription, Twilio proxy (`voice/recordings.py`)
- Contact data fetching and engagement metrics (`voice/contacts.py`)
- AI contact intelligence routes (`voice/intelligence.py`)
- A2P 10DLC registration routes (`voice/a2p.py`)
- Phone number management and Trust Hub (`voice/numbers.py`)
- Multi-line dialer and predictive dialing (`voice/dialer.py`)
- Predictive engine with Erlang-C pacing (`voice/predictive_engine.py`)

### Redis-Backed Call State (`voice/redis_state.py`)

Call state was migrated from in-memory Python dicts to Redis for cross-service sharing (preparation for FastAPI voice WebSocket extraction). Both Flask HTTP routes and future FastAPI WebSocket handlers read/write the same state.

**Redis key patterns:**

| State | Redis Key | Type | TTL |
|-------|-----------|------|-----|
| `active_calls` | `call:{call_sid}` | JSON string (SET/GET) | 3600s (1hr) |
| `transfer_requests` | `xfer:{call_sid}` | JSON string (SET/GET) | 30s |
| `overflow_transfer_alerts` | `overflow:{location_id}` | List (JSON items) | 60s |

**Why JSON strings, not Redis hashes:** `active_calls` dicts contain nested data (`_stir_verstat`, `_amd_result`, etc.). Redis hashes can't store nested values. Using `SET`/`GET` with `json.dumps()`/`json.loads()` preserves the full dict structure.

**Sync API** (Flask calls these via `voice.redis_state` or re-exported from `voice.call_state`):
- `set_active_call(call_sid, data)`, `get_active_call(call_sid)`, `update_active_call(call_sid, **fields)`, `delete_active_call(call_sid)`
- `get_all_active_calls()` — SCAN-based iteration (safe for production, no KEYS blocking)
- `get_active_calls_for_location(location_id)` — filters by `_location_id` field
- `set_transfer_request(call_sid, data)`, `get_transfer_request(call_sid)`, `delete_transfer_request(call_sid)`
- `add_overflow_alert(location_id, alert)`, `get_overflow_alerts(location_id)`, `dismiss_overflow_alert(location_id, call_sid)`

**Async API** (for future FastAPI — wraps sync via `asyncio.to_thread()`):
- `async_set_active_call()`, `async_get_active_call()`, `async_update_active_call()`, `async_delete_active_call()`
- `async_get_transfer_request()`, `async_set_transfer_request()`, `async_delete_transfer_request()`
- `async_add_overflow_alert()`, `async_get_overflow_alerts()`

**What stays in-process:**
- `call_listeners` (dict of `call_sid → set(queue.Queue)`) — audio frames are too high-volume for Redis. Stays in `voice/call_state.py` as a plain dict.
- `custom_field_defs` — simple per-location cache, rarely changes.

**What was removed:**
- Daemon reaper thread — Redis TTLs handle auto-cleanup automatically.
- Direct dict access (`active_calls[sid] = {...}`) — replaced with function calls across 8 voice files.

**Files migrated from in-memory dicts to Redis state functions:**
- `voice/twiml_routes.py` — inbound/outbound TwiML, call status updates
- `voice/outbound.py` — call initiation, status callbacks
- `voice/call_history.py` — status polling, hangup, takeover
- `voice/dialer.py` — multi-dial, active-lines, multi-hangup, multi-status
- `voice/voice_tools.py` — transfer signals, overflow alerts
- `voice/helpers.py` — call ownership verification
- `voice/recordings.py` — removed unused `active_calls` import
- `voice/stream.py` — still uses direct dict access (will be replaced by `async_stream.py` in Phase 2)

---

## Call Listen & Intercept (voice/stream.py + dialer.js)

### What It Is
Real-time call monitoring and agent takeover. Agents can listen to live AI-handled calls and intercept (take over) the call from the AI at any time.

### Architecture

**Listen (live audio stream):**
- Agent clicks Listen → opens WebSocket to `WS /voice/listen-stream`
- Server registers a `queue.Queue` in `call_listeners[call_sid]` dict
- Twilio media stream pushes audio frames → server copies each frame to all listener queues
- When the call ends, server pushes a `None` sentinel to all listener queues for instant exit (no 2-second timeout wait)
- Client plays audio via Web Audio API (AudioContext + mulaw decoding)

**Intercept (agent takeover):**
- Agent clicks Intercept → `POST /voice/takeover` with `{call_sid, location_id}`
- Server pre-fetches live call status from Twilio API before attempting redirect
- If call already ended (completed/busy/canceled/failed/no-answer), returns 400 with `"already ended"` message — avoids Twilio 400 explosion
- If call is live, redirects Twilio call to agent's TwiML app conference
- On successful intercept, frontend stops the listen stream (agent is now on the call directly)

### Race Condition Guards

**Stale WebSocket protection (frontend):**
- `_listenWs` reference tracked globally; all `onmessage`/`onclose`/`onerror` handlers check `if (ws !== _listenWs) return` before processing
- Prevents old socket events from interfering with new connections
- `_listenReconnectTimer` prevents duplicate reconnect timers from stacking up
- `_stopListenStream()` sets `onclose = null` before closing socket so the close event doesn't trigger a reconnect

**Intercept resilience (frontend):**
- `retries: 0` on takeover fetch (prevents double-redirect on failure)
- On 400 with "already ended" in response, shows disabled "Call Ended" button state instead of misleading "Intercept" reset
- Successful intercept auto-stops listen stream

**Server-side cleanup (voice/stream.py):**
- `'busy'` added to terminal call state check (was previously missing)
- `None` sentinel pushed to all listener queues when Twilio stream ends, ensuring listeners exit immediately

### Key Functions
| Location | Function | Purpose |
|----------|----------|---------|
| `voice/stream.py` | `listen_stream()` | WebSocket endpoint — registers listener queue, streams audio frames |
| `voice/stream.py` | `takeover()` | Pre-validates call status, then redirects to agent conference |
| `dialer.js` | `_startListenStream()` | Opens WebSocket, plays audio via AudioContext, handles reconnect |
| `dialer.js` | `_stopListenStream()` | Closes WebSocket with stale-socket guards, cleans up AudioContext |
| `dialer.js` | `dialerTakeover()` | Calls `/voice/takeover`, handles ended-call state, stops listen on success |

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

**Solo Predictive** (Erlang-C M/M/N queue model with AI Overflow + Auto-Pacing):

Built for solo agents working their own leads. Agent selects leads, adds to queue, hits "Dial" — the system auto-paces batches using Erlang-C math. When multiple calls answer simultaneously, the first bridges to the human agent and the rest overflow to AI voice agents.

**Auto-Pacing Flow:**
1. Agent selects leads → adds to queue → clicks "Dial"
2. Frontend fetches `GET /voice/predictive-stats` to get Erlang-C recommendation
3. System auto-dials the recommended number of lines (e.g., 3 lines at 15% connect rate)
4. As calls complete, system auto-fires next batch from queue
5. Every 15 seconds, frontend re-fetches live pacing with session data (calls made, connected, avg duration)
6. Erlang-C blends historical 7-day data + live session data (session weighted more as sample grows)
7. If connect rate changes mid-session, system adjusts lines up or down automatically

**Live Session Pacing** (`calculate_live_pacing()` in `predictive_engine.py`):
- Blends historical and session metrics with exponential weighting
- At 0 session calls: 100% historical. At 20+ calls: ~80% session data
- Confidence levels: low (<5 calls), medium (5-20), high (20+)
- Frontend passes `session_calls_total`, `session_calls_connected`, `session_avg_handle_time` as query params

**AI Overflow Collision Detection:**
1. Multiple calls answer simultaneously → each hits `POST /voice/outbound-twiml`
2. `agent_state_manager.try_claim_for_call()` atomically claims agent (thread-safe lock)
3. First call wins → bridges to human agent via `<Dial><Client>`
4. Subsequent calls see agent ON_CALL → check for available team members first
5. If team member READY → bridge to them (`get_any_available_agent()` fallback)
6. If nobody available → overflow to AI via `<Connect><Stream>` to xAI Realtime API

**AI Minutes Warning System:**
- `multi_dial()` checks AI minutes balance before each batch (overflow uses AI minutes)
- Warning levels: `ok` (>100 min), `low` (20-100), `critical` (<20), `empty` (0)
- At `empty`: forces single-line mode (no overflow possible without AI minutes)
- Frontend shows persistent toast with link to top up
- Periodic check during session via `GET /voice/ai-minutes-check`

**Overflow Transfer Alerts:**
- When AI overflow call has a hot lead wanting transfer but agent is ON_CALL:
  1. Alert logged to `overflow_transfer_alerts` in-memory dict
  2. Frontend polls `GET /voice/overflow-alerts` every 2 seconds
  3. Notification card slides in: "Hot Lead: [Name] wants to talk to you"
  4. Agent can "Accept Transfer" (calls `/voice/takeover`) or "Let AI Book"
  5. AI stalls naturally for 30 seconds with conversation while waiting
  6. After 30s timeout, AI transitions to booking an appointment
- `voice_tools.py` transfer_to_agent returns a stall response instead of immediate booking pivot

**Team Member Awareness (Contingent):**
- `AgentStateManager.get_any_available_agent(location_id, exclude_email)` — checks if any OTHER team member is READY
- Collision detection in `twiml_routes.py` checks team fallback before AI overflow
- Solo agents (99% of cases): unchanged behavior — `try_claim_for_call()` with single email
- Team locations: overflow tries team members first, AI last

### Frontend State
- `_multiLineActive` (Map) — tracks all concurrent call SIDs with contact info
- `_multiLineEnabled` (bool) — set from subscription tier check on page load
- `_multiLineMaxLines` (int) — max concurrent lines (1 for individual, 4 for pro_dialer)
- `_multiLineConnectedSid` (string) — which call the agent is currently interacting with
- `_predictiveStats` (object) — cached predictive analytics from server
- `_isSoloPredictive` (bool) — true when `subscription_tier === 'solo_predictive'`
- `_sessionCallsTotal` / `_sessionCallsConnected` / `_sessionTotalTalkTime` — session tracking for live pacing
- `_predictiveRecommendedLines` (int) — live Erlang-C recommendation, updated every ~15s
- `_aiMinutesWarning` (string) — current AI minutes warning level
- `_overflowAlertShown` (object) — tracks shown overflow transfer alert notifications

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
Used by both `solo_predictive` (solo agents with AI overflow) and `predictive_dialer` (agency-level) tiers:
- **Phone number parsing**: `area_code_to_timezone(phone)`, `area_code_to_state(phone)` — digits-only extraction, US country code removal for 11-digit numbers, ~300 NANP area code mappings
- **Timezone enforcement**: `check_recipient_timezone(phone, start, end)` — pytz-based, midnight wrap-around support
- **Recording consent**: `is_two_party_consent_state(phone)` — 12 two-party consent states (CA, CT, DE, FL, IL, MD, MA, MT, NV, NH, PA, WA)
- **Erlang-C pacing**: `calculate_optimal_dial_ratio()` — M/M/N queue model with binary search, TCPA throttle at 80% of limit
- **Solo Predictive pacing**: `calculate_solo_predictive_ratio()` — wraps Erlang-C with effective_agents = 1 human + N AI overflow lines
- **Live session pacing**: `calculate_live_pacing()` — blends 7-day historical + live session data with exponential weighting (session data weighted more as sample grows). Returns blended dial ratio, confidence level (low/medium/high), and session connect rate. Used by `GET /voice/predictive-stats?session_calls_total=&session_calls_connected=&session_avg_handle_time=`
- **TCPA tracker**: `TCPAComplianceTracker` (global singleton `tcpa_tracker`) — thread-safe rolling 30-day abandon rate, auto-prune, DB bootstrap via `load_from_db()`
- **Agent state machine**: `AgentStateManager` (global singleton `agent_state_manager`) — ACD states (Ready/Not Ready/On Call/Wrap-Up/Break/Extended Away/Logged Out), auto wrap-up→ready transitions, predicted availability within N-second horizon
- **Team member fallback**: `AgentStateManager.get_any_available_agent(location_id, exclude_email)` — finds first READY team member at a location (excluding the primary dialer). Used in collision detection to try team members before AI overflow
- **Callback queue**: `CallbackQueue` (global singleton `callback_queue`) — thread-safe scheduled re-dial queue with duplicate prevention, 24-hour auto-prune of completed/cancelled items
- **Compliance metrics**: `get_compliance_metrics()` — aggregated compliance score (0-100) from TCPA, DNC violations, calling hours violations
- **Overflow transfer alerts**: Redis-backed alerts in `redis_state.py` (key `overflow:{location_id}`, TTL 60s) — when AI overflow calls have hot leads wanting transfer. Polled by frontend via `GET /voice/overflow-alerts`

---

## A2P 10DLC Compliance (twilio_provisioning.py + voice_bridge.py)

### What It Is
A2P 10DLC (Application-to-Person 10-Digit Long Code) is the carrier-mandated registration system for sending business SMS over standard phone numbers. Without registration, messages may be filtered or blocked by carriers.

### Architecture
- **ISV sub-account model**: Follows the Twilio ISV A2P onboarding flow. Each sub-account creates its own Secondary Customer Profile, EndUser, and Trust Product — all on the sub-account using `get_sub_account_client_native()`.
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

## Voice Integrity / Number Integrity (twilio_provisioning.py + voice/numbers.py)

### What It Is
Voice Integrity registers phone numbers with carrier spam analytics engines (AT&T/Hiya, T-Mobile/CallHub, Verizon) to remediate spam labels and improve call answer rates. This is separate from A2P 10DLC (which is for SMS).

### Architecture — ISV/Sub-Account Flow
Follows the Twilio ISV/Reseller with Subaccounts guide exactly:
https://www.twilio.com/docs/voice/spam-monitoring-with-voiceintegrity/voice-integrity-onboarding/voice-integrity-trust-hub-api-isvs-subaccounts

**Phase 1: Secondary Customer Profile** (on sub-account)
1. Find or create a Secondary Customer Profile (`policy_sid=RNdfbf3fae0e1107f8aded0e7cead80bf5`)
2. Link to Primary Business Profile on master account via EntityAssignment
3. Reuse existing approved profiles from Spam Protection or A2P registration when possible

**Phase 2: Voice Integrity Trust Product** (on sub-account)
1. Create EndUser with `type=voice_integrity_information` (use_case, business_employee_count, average_business_day_call_volume — all must be strings of positive integers)
2. Create Trust Product (`policy_sid=RN5b3660f9598883b1df4e77f77acefba0`)
3. Link Secondary Profile → Trust Product (EntityAssignment)
4. Link EndUser → Trust Product (EntityAssignment)
5. Assign phone numbers → Trust Product (ChannelEndpointAssignment)
6. Run evaluation, then submit for review (status → pending-review)

**State storage**: All Voice Integrity data stored in `voice_config["number_integrity"]` JSONB.

### Provisioning Functions (twilio_provisioning.py)
- `_find_primary_profile_sid()` — Discovers the Primary Business Profile on the master account
- `_find_or_create_secondary_profile()` — 3-tier fallback: reuse existing → discover approved → create new (with correct policy + link to Primary)
- `create_voice_integrity_trust_product()` — Full ISV flow: Secondary Profile + EndUser + Trust Product + EntityAssignments
- `assign_numbers_to_voice_integrity()` — Assigns phone numbers to both profile and Trust Product
- `submit_voice_integrity_for_review()` — Runs evaluation then submits (status → pending-review)

### voice_config.number_integrity Schema
```json
{
  "trust_product_sid": "BU...",
  "profile_sid": "BU...",
  "end_user_sid": "IT...",
  "status": "pending-review",
  "business_name": "Acme Insurance",
  "assigned_numbers": ["PN...", "PN..."],
  "assigned_count": 2,
  "registered_at": "2026-03-13T..."
}
```

### Routes (voice/numbers.py)
| Route | Method | Purpose |
|-------|--------|---------|
| `/voice/number-integrity/register` | POST | Create Trust Product, assign numbers, submit for review |
| `/voice/number-integrity/add-numbers` | POST | Add numbers to existing registration |
| `/voice/number-integrity/remove-number` | POST | Remove number from registration |
| `/voice/number-integrity/status` | GET | Check registration status |

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

## Phone Number Management (voice/numbers.py)

### Pricing Model
- **First 5 numbers FREE** with any subscription (`FREE_NUMBERS_ALLOWANCE = 5`)
- **Local numbers**: $0.90/mo each (`NUMBER_PRICE_CENTS = 90`)
- **Toll-free numbers**: $2.15/mo each (`TOLL_FREE_PRICE_CENTS = 215`)
- Numbers are purchased on the user's Twilio sub-account; Stripe payment reimburses platform costs

### Routes

| Route | Method | Purpose |
|-------|--------|---------|
| `/voice/numbers` | GET | List all phone numbers + free remaining count |
| `/voice/numbers/search` | GET | Search available numbers by area code, state, city, zip, contains |
| `/voice/numbers/buy` | POST | Buy single number (free if under allowance, 402 if payment required) |
| `/voice/numbers/checkout` | POST | Create Stripe session for single paid number |
| `/voice/numbers/complete-purchase` | POST | Verify Stripe payment and provision single number |
| `/voice/numbers/cart-checkout` | POST | Cart checkout: provision free numbers, create Stripe session for paid batch |
| `/voice/numbers/complete-cart-purchase` | POST | Verify cart payment and provision all paid numbers from cart |
| `/voice/numbers/release` | POST | Release (delete) a phone number from sub-account |
| `/voice/numbers/nickname` | POST | Set friendly nickname for a number |
| `/voice/numbers/set-primary` | POST | Set a number as primary caller ID |
| `/voice/number-health` | GET | Number health dashboard data |
| `/voice/number-health/toggle` | POST | Enable/disable smart number rotation |
| `/voice/number-health/set-status` | POST | Manually set number status (active/resting/frozen/warmup) |
| `/voice/licensed-states` | POST | Save agent's licensed states |
| `/voice/trust-hub` | GET | Trust Hub / carrier registration status |
| `/voice/trust-hub/save` | POST | Save business profile for Trust Hub |
| `/voice/spam-protection/register` | POST | One-click spam protection (CNAM + Trust Hub) |
| `/voice/spam-protection/status` | GET | Spam protection status |

### Cart System
- Frontend cart stored in `localStorage` (`numCart` key), persists across page reloads
- Cart button with count badge appears in Numbers tab header when items are in cart
- Cart overlay shows pricing breakdown: free numbers labeled, paid numbers with per-unit price
- `POST /voice/numbers/cart-checkout` splits items into free (provisioned immediately) and paid (Stripe session)
- After Stripe redirect, `POST /voice/numbers/complete-cart-purchase` verifies payment and provisions remaining numbers
- Stripe metadata: `purchase_type: "phone_number_cart"`, `phone_numbers` (comma-separated), `total_cents`

---

## Dashboard Theme System

### Light/Dark Toggle
- **Toggle button**: Sun/Moon icon in topbar (`#themeToggleBtn`)
- **JavaScript**: `toggleTheme()` in `sidebar.js`, persisted via `localStorage` key `dash_theme`
- **CSS class**: `body.light-theme` activates all light mode overrides
- **CSS variables**: Light theme defines `--lt-*` variables (bg, surface, border, text, accent, input colors)

### Coverage
The light theme has comprehensive overrides for:
- Sidebar, topbar, glass panels, form inputs, dropdowns
- Dialer columns, contact rows, message bubbles, date separators
- iPhone/iOS UI (nav bar, thread scroll, composer, tab bar, call log, voicemail)
- Billing plan cards, pricing cards, metallic price text
- Discord panel, Slack panel, modals, tooltips, toasts
- Smart Filters, AI intelligence panels, workflow tabs
- Scrollbars, carrier chips, CRM buttons, advanced settings toggles
- All inline dark backgrounds/text colors overridden via attribute selectors

---

## White-Label Branding (Agency Owners)

Agency owners can fully white-label the dashboard so their agents see the agency's brand instead of InsuranceGrokBot.

### What's Customizable
- **Company name**: Replaces "InsuranceGrokBot" throughout the entire logged-in experience
- **Color scheme**: Primary accent color (replaces `#00ff88`), applied to buttons, links, highlights, sidebar accents
- **Font**: Choose from curated font list, applied to all dashboard text

### How It Works
- **Settings tab**: `dashboard/tabs/whitelabel.html` — agency owners pick name, color, font with live preview
- **JavaScript**: `static/js/dashboard/whitelabel.js` — live preview, color picker, font selector, save to server
- **Storage**: `agency_billing.whitelabel_config` JSONB column (`company_name`, `accent_color`, `font_family`)
- **CSS injection**: On dashboard load, if user belongs to an agency with whitelabel config, CSS custom properties are overridden dynamically
- **Scope**: Only affects logged-in dashboard experience — marketing pages remain InsuranceGrokBot branded

### Agency Dashboard Integration
The agency dashboard is now integrated as tabs within the main dashboard (not a separate page). Agency owners see:
- All individual dashboard features (dialer, voice, SMS config, workflows, etc.)
- **Agency Members tab** (`dashboard/tabs/agency_members.html`) — view/manage all agency users
- **Agency KPIs tab** (`dashboard/tabs/agency_kpis.html`) — comprehensive tiled statistics dashboard with Chart.js charts
- **White-label tab** (`dashboard/tabs/whitelabel.html`) — branding customization

### Agency Owner Access
- Agency owners are stored in the `agency_billing` table with `role = 'agency_owner'`
- To manually upgrade a user to agency owner: INSERT into `agency_billing` with `role='agency_owner'`, `agency_email`, `company_id`, `password_hash`, and `location_id`
- Agency owners log in via `/agency-login` which queries `agency_billing` only
- Agency owners are FREE — no subscription paywall required
- Regular login (`/login`) also detects agency owners and redirects to `/agency-dashboard`

### Agency KPI Dashboard (Tiled Layout with Charts)
The Agency KPIs tab provides insurance agency-specific statistics in a tiled layout with Chart.js visualizations:

**Core Metric Tiles (6-up grid):**
- Total Calls, Connected, Messages Sent, Active Agents, Unique Contacts, Avg Speed to Lead

**Chart Row (3 cards):**
- **Connect Rate Donut** — doughnut chart showing connected vs not connected percentage
- **Call Quality Donut** — duration bucket breakdown (<45s, 45s–2m, 2–5m, 5–10m, 10m+)
- **Daily Volume Bar Chart** — daily calls and connected calls over the selected period

**Duration Bucket Tiles (4-up):**
- Over 45 Seconds, Over 2 Minutes, Over 5 Minutes, Over 10 Minutes — each with count, percentage, and animated fill bar

**Agency Averages (4-up):**
- Avg Daily Dials per Agent, Avg Daily Dials (Agency total), Avg Connect Rate per Agent, Total Talk Time

**Top Performers Leaderboards (3-up):**
- Highest Connect Rate (top 5, min 5 calls to qualify)
- Most Active by Calls (top 5)
- Best Conversations by Avg Duration (top 5, min 3 connected calls)
- Each shows rank medal (gold/silver/bronze), agent name, and key stat

**All Agent Breakdown:**
- Expandable table via "See All Agents" button
- Columns: Agent, Calls, Connected, Rate, Avg/Day, Talk Time, Avg Duration, >45s, >2m, >5m, Messages

**Hourly Heatmap:**
- Bar chart showing call volume by hour (12a–11p) with connected overlay

**API endpoint:** `GET /api/agency/dashboard-stats?period=today|week|month|all`

### Agency Auto-Import by Company ID
- When an individual agent subscribes and their GHL `companyId` matches an agency owner's `company_id` in `agency_billing`, they are automatically linked to that agency
- No manual invite required — agents appear in the agency dashboard as they subscribe
- Agency owners can still manually invite via email as a fallback

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
- **24-hour cache**: Results cached in `contact_intelligence` table (JSONB) with 24h TTL. Cache also invalidated when new messages arrive after analysis. On every dialer load, ALL contacts are queued for fresh analysis — server skips contacts with fresh cache (<24h, no new messages). ~1000 contacts analyzed in ~30 seconds.
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
2. Bulk AI intelligence fetched from cache (`/voice/contact-intelligence-bulk`) — shows cached data instantly
3. Contacts grouped by AI temperature into Smart Filter categories
4. ALL contacts queued to RQ workers via `POST /voice/contact-intelligence-analyze` — server skips fresh cache (<24h)
5. Frontend polls bulk endpoint every 2s — as workers complete analysis, contacts update in real time
6. Cache has 24h TTL; stale contacts re-analyzed automatically on next dialer load
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

## Main Navigation Structure (base.html)

The marketing site navbar uses two dropdown menus:
- **Agents** → `/for-individuals`
- **Agencies** → `/for-agencies`
- **Features** (dropdown): SMS AI Texting (`/sms`), AI Workflows (`/workflows`), Spam Protection (`/spam-protection`)
- **Resources** (dropdown): Comparison (`/comparison`), Articles (`/articles`), Connect With Us (`/connect`)
- **Pricing** → `/#pricing`
- **Demo** → `/demo-chat`
- **Support** → `/support`

Footer links include: For Agents, For Agencies, SMS, Workflows, Spam Protection, Comparison, Articles, About, Support, Contact, Connect With Us, Affiliates, Terms, Privacy.

### SEO Articles (`/articles/*`)

Five long-form SEO articles, each with their own page, JSON-LD structured data, and meta descriptions:

| URL | Topic | SEO Target |
|-----|-------|------------|
| `/articles/top-tools-life-insurance-agents` | Best tools for insurance agents in 2026 | "tools for life insurance agents" |
| `/articles/ai-life-insurance-good-thing` | Why AI benefits insurance agents | "AI life insurance" |
| `/articles/gohighlevel-vs-hubspot-insurance` | CRM comparison for insurance | "GoHighLevel vs HubSpot insurance" |
| `/articles/speed-to-lead-insurance` | Speed to lead research & ROI math | "speed to lead insurance" |
| `/articles/spam-likely-killing-sales` | Spam protection playbook (CNAM, STIR/SHAKEN, Voice Integrity, A2P) | "spam likely insurance agent" |

All articles use the `.article-page` layout with `.article-section`, `.article-highlight-box`, `.article-stat-row`, `.article-cta-box` CSS components. Light theme overrides included.

### Social Media / Connect Page (`/connect`)

Links to InsuranceGrokBot social profiles:
- **Facebook**: `https://www.facebook.com/profile.php?id=61587536844180`
- **LinkedIn**: `https://www.linkedin.com/company/insurancegrokbot`

Uses `.connect-grid`, `.connect-card`, `.connect-reason-card` CSS components with Liquid Glass styling. Includes JSON-LD Organization schema with `sameAs` social links.

---

## Frontend (Dashboard)

### Template Structure
```
templates/
  dashboard.html              Main dashboard layout (individual user)
  agency-dashboard.html       Agency command center (deprecated — agency features now integrated into main dashboard)
  base.html                   Base template for marketing pages
  home.html                   Home/landing page
  sms.html                    SMS marketing page
  workflows-marketing.html    Workflow automation marketing page
  spam-protection.html        Spam monitoring & number health marketing page
  comparison.html             Main comparison page
  comparison-text-drip.html   Text drip comparison page
  comparison-dialers.html     Dialer comparison page
  for-agencies.html           Agency landing page
  for-individuals.html        Individual agent landing page
  a2p-guide.html              A2P 10DLC registration guide
  about.html                  About page
  affiliate.html              Affiliate program page
  articles.html               Articles/blog listing page (7 articles)
  article-top-tools-life-insurance.html          SEO: Top Tools for Life Insurance Agents
  article-ai-life-insurance-good-thing.html      SEO: Why AI in Life Insurance Is a Good Thing
  article-gohighlevel-vs-hubspot-insurance.html  SEO: GoHighLevel vs HubSpot for Insurance
  article-speed-to-lead-insurance.html           SEO: Speed to Lead in Insurance Sales
  article-spam-likely-killing-sales.html         SEO: How Spam Likely Is Killing Your Sales
  connect.html                Connect With Us / social media page
  predictive-dialer.html      Predictive Dialer marketing page
  white-label.html            White-label marketing page
  claim_seat.html             Team member claim seat flow
  uninstall-feedback.html     GHL marketplace uninstall feedback
  call_panel.html             Embedded call panel
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
    team.html                 Team management (invite, roles, permissions, KPIs)
    training.html             Training API configuration
    whitelabel.html           White-label branding settings (agency owners only)
    agency_kpis.html          Agency KPIs dashboard tab
    agency_members.html       Agency member management tab
  dashboard/modals/
    discord_panel.html        Discord message panel (side panel, hidden)
    discord_modal.html        Discord server picker modal
    slack_panel.html          Slack message panel (side panel, hidden)
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
  slack.js          Slack integration (workspace/channel management, messages)
  team.js           Team management (invites, roles, permissions, agent KPIs, audit log)
  pwa.js            Progressive Web App registration and offline support
  tutorial.js       Interactive dashboard tutorial (driver.js, 7 chapters, Liquid Glass UI with dark+light theme)
  whitelabel.js     White-label branding UI (color picker, font selector, company name, live preview)
  agency.js         Agency dashboard — comprehensive KPIs with Chart.js (donut charts, bar charts, leaderboards, duration buckets, all-agent table)
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

## Slack Integration

The dashboard embeds a Slack workspace chat panel (similar to Discord):
1. User connects Slack via OAuth (`/slack/connect`)
2. User adds Slack workspaces
3. User clicks a channel in the sidebar → chat panel slides open
4. Messages displayed in real-time; users can read and reply without leaving the dashboard

### Slack Panel Position
- Fixed panel that slides out from the right side of the sidebar (mirrors Discord panel UX)
- Toggle: click a channel in the sidebar
- Dismissible with the X button in the panel header

---

## Team Management

Multi-user team support for individual subscribers (distinct from Agency model):
1. Account owner invites team members via email (`POST /api/team/invite`)
2. Invitees receive email with claim link → `/claim-seat` sets password and activates account
3. Owner manages roles (admin/agent/viewer), permissions, and active status
4. Each team member can get their own Twilio sub-account for voice (`POST /api/team/activate-voice`)
5. Per-agent KPIs, stats, and audit logging for all team management actions

### Key Tables
- `location_users` — Team members per location (role, permissions JSON, invited/claimed status)
- `team_audit_log` — Audit trail for invites, role changes, permission updates, removals

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

## CRM Provider Plugin System (`crm_providers/`)

A higher-level abstraction above CRM adapters. Providers handle the full integration lifecycle: OAuth, webhooks, data sync, activity logging, contact resolution, and embeddable UI. The adapter layer handles individual API operations; the provider layer orchestrates them.

### Architecture

```
crm_providers/
├── __init__.py          Provider registry + get_provider(crm_type)
├── base.py              CRMProvider ABC (full interface definition)
├── ghl/
│   └── __init__.py      GHLProvider — wraps existing GHL code (zero changes to originals)
└── hubspot/
    ├── __init__.py      HubSpotProvider — orchestrator wiring all pieces
    ├── oauth.py         OAuth2 flow (initiate, callback, token refresh)
    ├── inbound.py       Webhook handler (batched, HMAC-SHA256 v3)
    ├── sync.py          Data sync engine (conversations, deals, contacts)
    ├── logger.py        Activity logging (SMS, calls, notes → HubSpot timeline)
    ├── resolver.py      Contact search via CRM v3 Search API
    └── crm_card.py      CRM Card endpoint (AI intelligence sidebar)
```

### Provider Registry

```python
from crm_providers import get_provider
provider = get_provider("hubspot")  # Returns HubSpotProvider singleton
provider = get_provider("ghl")      # Returns GHLProvider singleton
```

Lazy-loaded singletons. Provider type determined by `subscribers.crm_type` column (`"ghl"` or `"hubspot"`).

### CRMProvider ABC Interface

| Method | Purpose |
|--------|---------|
| `normalize_webhook(request_data, headers)` | Convert raw CRM webhook → canonical `CRMEvent` |
| `verify_webhook_signature(data, headers, sig)` | Verify webhook authenticity |
| `get_webhook_event_type_map()` | Map IGB trigger names → CRM-specific event types |
| `get_valid_token(subscriber)` | Get valid API token, auto-refresh if expired → `(token, was_refreshed, error)` |
| `refresh_token(subscriber)` | Force-refresh OAuth token |
| `sync_conversations(location_id, token)` | Sync messages → `crm_conversations` table |
| `sync_deals(location_id, token)` | Sync deals → `crm_deals` table |
| `log_outbound_sms(contact_id, message, token)` | Log SMS to CRM timeline |
| `log_call(contact_id, direction, duration, token)` | Log call to CRM timeline |
| `resolve_contact(phone, name, email, token, location_id)` | Search CRM for contact |

### CRM-Agnostic Database Tables

The integration uses renamed tables with `crm_source` columns:

| Table | Replaces | crm_source values |
|-------|----------|-------------------|
| `crm_conversations` | `ghl_conversations` | `ghl`, `hubspot` |
| `crm_deals` | `ghl_opportunities` | `ghl`, `hubspot` |
| `crm_sync_state` | `ghl_sync_state` | `ghl`, `hubspot` |
| `contacts` | (new) | `ghl`, `hubspot` |

Column renames: `ghl_message_id` → `external_message_id`, `ghl_opportunity_id` → `external_deal_id`.

---

## HubSpot Deep Integration

### Overview

Full self-contained HubSpot CRM integration in `crm_providers/hubspot/`. Brings IGB's AI texting, voice, lead intelligence, and Smart Filters to HubSpot users. All HubSpot code is isolated in the provider package — zero changes to existing GHL code paths.

### HubSpot OAuth2 Flow

- **Initiate**: `GET /hubspot/oauth/initiate` → redirects to `https://app.hubspot.com/oauth/authorize` with scopes + CSRF state param
- **Callback**: `GET /hubspot/oauth/callback` → exchanges code for tokens via `POST https://api.hubapi.com/oauth/v1/token`
- **Token storage**: `subscribers.crm_config` JSONB with `access_token`, `refresh_token`, `token_expires_at`, `hub_id`
- **Token expiry**: HubSpot tokens expire every **6 hours** (vs GHL's 24 hours)
- **Auto-refresh**: 10-minute buffer before expiry; cron job refreshes proactively every 15 minutes
- **Redirect URI**: `https://{YOUR_DOMAIN}/hubspot/oauth/callback`

### Required OAuth Scopes

```
crm.objects.contacts.read
crm.objects.contacts.write
crm.objects.deals.read
crm.objects.deals.write
crm.objects.communications.read
crm.objects.communications.write
crm.objects.meetings.write
timeline
```

### HubSpot Webhook Events

| HubSpot Event | IGB Mapping | Purpose |
|---------------|-------------|---------|
| `contact.creation` | `ContactCreate` | New lead created |
| `contact.propertyChange` | `ContactUpdate` | Contact field changed (includes SMS activity) |
| `deal.creation` | `DealCreate` | New deal/opportunity |
| `deal.propertyChange` | `DealUpdate` | Deal stage changed |

**Webhook endpoint**: `POST /hubspot/webhook`
**Signature**: HMAC-SHA256 v3 — `HMAC(client_secret, POST + url + body + timestamp)`
**Headers**: `X-HubSpot-Signature-v3`, `X-HubSpot-Request-Timestamp`
**Payload**: JSON array of batched events (caller iterates batch)

### HubSpot Data Sync

Syncs HubSpot data into local Postgres (mirrors `ghl_sync.py` patterns):
- **Conversations**: `GET /crm/v3/objects/communications` → `crm_conversations` table
- **Deals**: `GET /crm/v3/objects/deals` + pipeline stages → `crm_deals` table
- **Contacts**: `GET /crm/v3/objects/contacts` → `contacts` table

Features: cursor-based pagination (`after` param), exponential backoff, 429 rate limit handling, 401 auto-refresh, UPSERT via `INSERT ... ON CONFLICT DO UPDATE`.

**Rate limits**: 40 req/10s (OAuth apps), 100 req/10s (private apps).

### HubSpot Activity Logging

Logs IGB activity back to HubSpot timeline so agents see everything in their CRM:

| IGB Action | HubSpot Object | Association Type ID |
|------------|---------------|---------------------|
| Outbound SMS | Communication (`hs_communication_channel_type: SMS`) | 81 |
| Call | Call engagement (direction, duration, recording URL) | 194 |
| AI Note | Note (intelligence summary) | 202 |

### HubSpot CRM Card

Displays AI intelligence in the HubSpot sidebar when agents view a contact:
- **Data fetch URL**: `GET /hubspot/crm-card?associatedObjectId={id}&portalId={id}`
- **Returns**: CRM Card v3 JSON with temperature, score, summary, recommended actions
- **Zero AI cost**: reads from `contact_intelligence` cache only
- **Actions**: "Full AI Intelligence" (IFRAME → `/embed/intelligence/{id}`) and "Open Dialer" (IFRAME → `/embed/dialer?contact={id}`)

### HubSpot Contact Resolution

- `POST /crm/v3/objects/contacts/search` with OR filter groups
- Priority: email > phone > name
- Phone normalization: strips to digits, removes leading country code `1`

### Embeddable Panel Routes (`blueprints/embed.py`)

Lightweight iframe-friendly routes for embedding IGB inside HubSpot CRM Cards:
- `GET /embed/panel` — Mini-dashboard (no sidebar/topbar)
- `GET /embed/dialer?contact={id}` — Dialer focused on one contact
- `GET /embed/intelligence/{contact_id}` — AI intelligence card (temperature, score, summary, actions)
- `GET /embed/intelligence/{contact_id}/json` — JSON API for intelligence data
- Template: `templates/embed_base.html` (dark theme, minimal chrome)

### Workflow Engine CRM Abstraction

The workflow engine (`workflow_engine.py`) has been refactored for multi-CRM support:
- `_is_ghl(crm_type)` helper branches by CRM type
- `_fetch_contact()` uses CRM adapter for non-GHL contacts
- 7 step handlers are CRM-aware: `_handle_send_sms`, `_handle_add_tag`, `_handle_remove_tag`, `_handle_update_field`, `_handle_add_note`, `_handle_assign_agent`, `_handle_move_stage`
- `TRIGGER_EVENT_MAP_BY_CRM` maps trigger names to per-CRM event types
- GHL code paths remain completely untouched

### Contact Import CRM Abstraction

`import_contacts_task()` in `tasks.py` detects CRM type and uses adapter for non-GHL:
- Non-GHL: `adapter.search_contact()` for dedup, `adapter.create_contact()`/`adapter.update_contact()` for CRUD
- GHL: existing direct API path unchanged

---

## HubSpot Developer App Setup Guide

### Prerequisites

- HubSpot Developer account at https://developers.hubspot.com
- Node.js 18+ installed (for HubSpot CLI)
- IGB platform deployed with public HTTPS domain (`YOUR_DOMAIN` env var)
- `HUBSPOT_CLIENT_ID` and `HUBSPOT_CLIENT_SECRET` env vars ready to set

### Step 1: Create HubSpot Developer Account

1. Go to **https://developers.hubspot.com** and sign up / log in
2. This gives you a **developer account** — distinct from a regular HubSpot portal
3. From the developer dashboard, create a **developer test account** (free, 90-day Enterprise trial) for testing installs

### Step 2: Install HubSpot CLI

```bash
npm install -g @hubspot/cli@latest
hs --version  # verify
```

### Step 3: Authenticate CLI

```bash
hs account auth
# Select your developer account
# Creates hubspot.config.yml with credentials
```

You'll need a **Personal Access Key** with these permissions enabled:
- `Developer projects`
- `Developer test accounts`
- `File manager`

### Step 4: Create the Public App

```bash
hs project create
# Project name: insurancegrokbot-hubspot
# App type: Public App
```

This generates a project with `app-hsmeta.json` and `extensions/` folder.

### Step 5: Configure `app-hsmeta.json`

This is the single source of truth for your HubSpot app. Configure it with IGB's requirements:

```json
{
  "version": "1.0.0",
  "name": "InsuranceGrokBot",
  "description": "AI-powered SMS and voice bot for insurance agents",
  "uid": "insurancegrokbot",
  "auth": {
    "required_scopes": [
      "crm.objects.contacts.read",
      "crm.objects.contacts.write",
      "crm.objects.deals.read",
      "crm.objects.deals.write",
      "crm.objects.communications.read",
      "crm.objects.communications.write"
    ],
    "optional_scopes": [
      "crm.objects.meetings.write",
      "timeline"
    ],
    "scopes_prompt": "oauth",
    "redirect_urls": [
      "https://{YOUR_DOMAIN}/hubspot/oauth/callback"
    ]
  },
  "webhooks": {
    "implementation_language": "backend",
    "target_url": "https://{YOUR_DOMAIN}/hubspot/webhook",
    "subscriptions": [
      "contact.creation",
      "contact.propertyChange",
      "deal.creation",
      "deal.propertyChange"
    ]
  },
  "extensions": [
    {
      "type": "crm/record-page/card",
      "location": "crm.record.property-panel",
      "objectTypes": ["CONTACT"],
      "title": "InsuranceGrokBot AI Intelligence",
      "data_fetch_uri": "https://{YOUR_DOMAIN}/hubspot/crm-card"
    }
  ]
}
```

Replace `{YOUR_DOMAIN}` with your actual domain (e.g., `app.insurancegrokbot.click`).

### Step 6: Deploy the App to HubSpot

```bash
hs project deploy
```

This registers your app with HubSpot. After deploy, go to the HubSpot Developer Portal and note:
- **Client ID** — copy this
- **Client Secret** — copy this

### Step 7: Set Environment Variables on IGB Platform

Add to your `.env` or deployment config:

```bash
HUBSPOT_CLIENT_ID=your_client_id_from_step_6
HUBSPOT_CLIENT_SECRET=your_client_secret_from_step_6
```

The `YOUR_DOMAIN` env var should already be set from your existing deployment.

### Step 8: Install on Test Account

1. In HubSpot Developer Portal, go to your app → **Testing** → **Install on test account**
2. Select your developer test account
3. Authorize the OAuth scopes
4. This triggers the OAuth flow: HubSpot → `GET /hubspot/oauth/initiate` → consent → `GET /hubspot/oauth/callback`
5. Tokens are stored in `subscribers.crm_config` automatically

### Step 9: Verify Webhook Delivery

1. In HubSpot Developer Portal, go to your app → **Webhooks** tab
2. Verify subscriptions are active: `contact.creation`, `contact.propertyChange`, `deal.creation`, `deal.propertyChange`
3. Create a test contact in your test HubSpot portal
4. Check IGB logs for `POST /hubspot/webhook` receiving the event
5. Verify HMAC-SHA256 v3 signature passes (uses `HUBSPOT_CLIENT_SECRET`)

### Step 10: Verify CRM Card

1. Open a contact in your test HubSpot portal
2. The right sidebar should show "InsuranceGrokBot AI Intelligence" card
3. First load shows "No AI intelligence available" (cache is empty)
4. After the bot processes conversations for this contact, the card populates with temperature, score, summary, and action buttons

### Step 11: Test End-to-End Flow

1. Create a contact in HubSpot test portal
2. Verify `contact.creation` webhook fires → IGB receives and queues it
3. Send a test SMS through IGB → verify Communication object appears in HubSpot timeline
4. Check data sync: trigger `/api/cron/sync-ghl-data` (it now also syncs HubSpot subscribers)
5. Verify `crm_conversations`, `crm_deals`, `contacts` tables have HubSpot data with `crm_source='hubspot'`
6. Trigger AI intelligence analysis → verify CRM Card shows data

### Step 12: Go Live (Production Install)

1. In HubSpot Developer Portal, submit app for **marketplace review** (if distributing publicly)
2. Or install directly on production HubSpot portals via the OAuth install URL:
   ```
   https://app.hubspot.com/oauth/authorize?client_id={CLIENT_ID}&redirect_uri={YOUR_DOMAIN}/hubspot/oauth/callback&scope=crm.objects.contacts.read%20crm.objects.contacts.write%20crm.objects.deals.read%20crm.objects.deals.write%20crm.objects.communications.read%20crm.objects.communications.write
   ```

### Important Notes

- **Classic CRM Cards sunset October 31, 2026** — the current implementation uses CRM Card v3 data fetch format which works now. Plan migration to UI Extensions (React components) before sunset.
- **Token refresh**: HubSpot tokens expire every 6 hours. The cron job at `/api/cron/refresh-tokens` handles this automatically. Schedule it every 15 minutes.
- **Rate limits**: OAuth apps get 40 requests per 10 seconds. The sync engine includes exponential backoff and 429 handling.
- **Webhook batching**: HubSpot sends events in batched arrays, not individually. The webhook handler iterates the batch.
- **`HUBSPOT_CLIENT_SECRET` is dual-purpose**: used for both OAuth token exchange AND webhook signature verification (HMAC-SHA256 v3).

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
- `lead_intelligence.py` fires one AI micro-prompt per contact returning temperature, score, should_respond, engagement_level, summary, and actions; caches in `contact_intelligence` table with 24h TTL + message-based invalidation. On every dialer load, ALL contacts are queued for fresh analysis — server skips fresh cache. ~1000 contacts in ~30 seconds via bulk LLM (25 per call, 10 concurrent)
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
- **Agency Owner**: FREE — no separate subscription. Agency owners buy the same individual plans (Power Dialer, Pro Dialer, or Predictive Dialer) as their agents. The agency dashboard, KPIs, leaderboards, and white-label features are included at no additional cost. Each agent under the agency purchases their own plan.
- **AI Minutes**: Add-on usage-based billing for AI voice processing

Subscriptions managed via Stripe. Users without active subscriptions see a paywall on the dashboard. Plan switching between Power Dialer, Pro Dialer, and Predictive Dialer is handled via `POST /change-plan` which calls `stripe.Subscription.modify()` with proration. There are no separate agency subscription tiers — agency owners use the same plans as individuals.

---

## Liquid Glass Design System

InsuranceGrokBot uses a unified **Liquid Glass** visual language across all UI surfaces. Every panel, card, modal, dropdown, and interactive element must adhere to these rules. Do not deviate.

### Core Concept

Liquid Glass simulates a physical pane of frosted glass lit from the top-left. It combines:
- **`backdrop-filter`** for blur + saturation (the "frosted" part)
- **Layered `rgba` backgrounds** for translucency
- **Asymmetric borders** — top/left edges are brighter (catch the light), bottom/right are darker (in shadow)
- **`inset` box-shadows** to simulate glass thickness and edge glow

### CSS Custom Properties

All glass values are defined in `:root` in `static/css/style.css`:

```css
/* Background gradients */
--glass-bg:         linear-gradient(135deg, rgba(255,255,255,0.08) 0%, rgba(255,255,255,0.02) 100%);
--glass-bg-strong:  linear-gradient(135deg, rgba(255,255,255,0.12) 0%, rgba(255,255,255,0.04) 100%);

/* Blur filter */
--glass-blur:       blur(20px) saturate(160%) brightness(1.1);

/* 3D Edge Lighting — top/left bright, bottom darker */
--glass-border-top:    rgba(255,255,255,0.25);
--glass-border-left:   rgba(255,255,255,0.15);
--glass-border-bottom: rgba(255,255,255,0.05);
--glass-border:        rgba(255,255,255,0.15);   /* legacy alias */

/* Depth shadows */
--glass-shadow:       0 16px 32px -8px rgba(0,0,0,0.5), inset 0 1px 1px rgba(255,255,255,0.15);
--glass-shadow-hover: 0 24px 48px -12px rgba(0,255,136,0.15), inset 0 1px 2px rgba(255,255,255,0.25);

/* Accent */
--accent: #00ff88;
--accent-hover: #ffffff;
--accent-dim: rgba(0,255,136,0.12);
```

### Canonical Glass Pattern

Apply this to any card, panel, dropdown, or modal:

```css
.my-glass-element {
    background: var(--glass-bg);
    backdrop-filter: var(--glass-blur);
    -webkit-backdrop-filter: var(--glass-blur);
    border-width: 1px;
    border-style: solid;
    border-color: var(--glass-border-top) var(--glass-border-bottom) var(--glass-border-bottom) var(--glass-border-left);
    /* shorthand: top  right  bottom  left */
    border-radius: 12px;
    box-shadow: var(--glass-shadow);
}
.my-glass-element:hover {
    background: var(--glass-bg-strong);
    box-shadow: var(--glass-shadow-hover);
}
```

### Light Theme Overrides

Light theme is activated via `body.light-theme`. All glass colors and surfaces have light-mode counterparts defined with `--lt-*` variables. Re-define any glass surface inside `body.light-theme { }`:

```css
body.light-theme .my-glass-element {
    background: rgba(255,255,255,0.85);
    border-color: rgba(0,0,0,0.10) rgba(0,0,0,0.06) rgba(0,0,0,0.06) rgba(0,0,0,0.08);
    box-shadow: 0 8px 24px rgba(0,0,0,0.08), inset 0 1px 0 rgba(255,255,255,0.9);
}
```

### Key `--lt-*` Variables (Light Theme)

```css
--lt-bg:            #f0f2f5;
--lt-surface:       rgba(255,255,255,0.82);
--lt-surface-hover: rgba(255,255,255,0.95);
--lt-border:        rgba(0,0,0,0.10);
--lt-text-primary:  #0d0d12;
--lt-text-secondary:#374151;
--lt-text-muted:    #6b7280;
--lt-text-faint:    #9ca3af;
--lt-input-bg:      rgba(255,255,255,0.70);
--lt-input-text:    #111827;
--lt-accent:        #00aa5e;
--lt-accent-bg:     rgba(0,170,94,0.10);
```

### Reference Implementation

The `Choices.js` custom select overrides in `static/css/style.css` (search for `/* Choices.js */`) are the canonical gold-standard implementation of a fully-themed Liquid Glass interactive component with dark and light variants. Model any new interactive components after that section.

---

## ⛔ NO INLINE STYLING — Mandatory Rule

**Inline styles are strictly prohibited in all Jinja2/HTML templates.**

### Why This Matters

Inline `style=""` attributes bypass the CSS variable system entirely. They cannot respond to `body.light-theme` toggling, causing elements to stay dark-colored in light mode. The extensive `[style*="background:#1a1a2e"]`-type attribute selectors currently in `style.css` are band-aid workarounds for exactly this problem — they prove that inline styles break theming.

### The Rule

> **Never write `style="..."` on any element in any `.html` template file.**

Instead:
1. Add a semantic CSS class to the element (e.g. `class="stat-card"`)
2. Define the visual properties for that class in `static/css/style.css`
3. Add the light-theme override inside `body.light-theme { }` in the same CSS file

### Allowed Exceptions

The only permitted inline style is for **JavaScript-driven dynamic values** that cannot be known at render time — e.g. a progress bar width driven by a data attribute:
```html
<!-- OK: value is dynamic, set by JS -->
<div class="progress-fill" style="width: 0%"></div>
```
```js
el.style.width = score + '%';  // JS sets it dynamically
```

**Everything else must use CSS classes.** If you find yourself writing `style="color:#888"` in a template, stop — add a class instead.

### Migrating Existing Inline Styles

When editing a template that has inline styles:
1. Extract the inline properties into a new class in `static/css/style.css`
2. Replace `style="..."` with `class="..."`
3. Add corresponding `body.light-theme .your-class` overrides
4. Remove the corresponding `[style*="..."]` band-aid selectors from `style.css`

This progressively eliminates the `[style*]` hacks and reduces CSS file size.

---

## ⛔ TWILIO DUAL ARCHITECTURE PROTOCOL — Mandatory Rule

**This platform uses TWO Twilio patterns. Use the correct one based on the account type.**

### The Two Patterns

1. **Master Account = Direct Customer** — The platform owner's own account. Uses the Primary Business Profile directly. No Secondary profiles. Detected via `is_master_account(sub_account_sid)`.

2. **Subscriber Sub-Accounts = ISV/Reseller with Sub-Accounts** — Each subscribing agency. Uses Secondary Customer Profile linked to master's Primary. All Trust Hub operations use the sub-account's own credentials (`get_sub_account_client_native()`).

### Why This Matters

Twilio's Trust Hub, Messaging, A2P, Voice Integrity, SHAKEN/STIR, and CNAM APIs all have distinct flows for Direct Customers vs ISV sub-accounts. Using the wrong flow causes bundle rejections, cross-account contamination, and compliance failures. The master account IS a Direct Customer. Sub-accounts ARE ISV customers.

### The Rules

> 1. **Check `is_master_account()` first** — every Trust Hub function must branch: Direct Customer flow for master, ISV flow for sub-accounts.
> 2. **Sub-accounts use `get_sub_account_client_native(sub_account_sid, sub_account_auth_token)`** for ALL TrustHub and Messaging API calls. Sub-accounts have their own SID + auth token and are fully independent. Never use master credentials for sub-account resources.
> 3. **Master account uses the Primary Business Profile directly** — no Secondary profile needed. The Primary is already approved in the Twilio Console.
> 4. **Sub-account Customer Profiles are SECONDARY profiles** — create with `policy_sid=RNdfbf3fae0e1107f8aded0e7cead80bf5` and link to the Primary Business Profile on the master account via EntityAssignment.
> 5. **Trust Products use their own policy SIDs** — CNAM uses `RNf3db3cd1fe25fcfd3c3ded065c8fea53`, Voice Integrity uses `RN5b3660f9598883b1df4e77f77acefba0`, A2P uses `RNb0d4771c2c98518d916a3d4cd70a8f8b`. Never use a Trust Product policy SID when creating a Customer Profile.
> 6. **Reuse existing approved Secondary Profiles** across A2P, Voice Integrity, SHAKEN/STIR, and CNAM on the same sub-account. Use `_find_or_create_secondary_profile()`.
> 7. **EndUser attributes must be strings of positive integers** — `business_employee_count` and `average_business_day_call_volume` must be `"10"`, `"500"`, etc. Never `"0"`, never ranges, never empty.
> 8. **Phone numbers must be assigned to BOTH the Customer Profile AND the Trust Product** — Twilio requires numbers on the profile before they can be assigned to a Trust Product.
> 9. **Use the Twilio SDK for all operations EXCEPT status transitions** — Evaluation, entity creation, assignments all use the SDK. Status transitions (`pending-review`) use `_trusthub_update_status()` (direct HTTP POST) to work around a known SDK bug where `.update(status=...)` silently drops the Status parameter.
> 10. **UI must reflect actual Twilio state** — Never mark numbers as "protected" based on local DB flags alone. Verify the profile/Trust Product actually exists on the correct account.

### Reference Documentation

Before writing or modifying any Twilio Trust Hub code, consult the correct guide for the account type:

**Direct Customer (master account):**
- **Voice Integrity Direct**: https://www.twilio.com/docs/voice/spam-monitoring-with-voiceintegrity/voice-integrity-onboarding/voice-integrity-trust-hub-api-direct-customer
- **CNAM**: https://www.twilio.com/docs/voice/brand-your-calls-using-cnam

**ISV/Reseller with Sub-Accounts (subscriber accounts):**
- **Voice Integrity ISV**: https://www.twilio.com/docs/voice/spam-monitoring-with-voiceintegrity/voice-integrity-onboarding/voice-integrity-trust-hub-api-isvs-subaccounts
- **A2P 10DLC ISV**: https://www.twilio.com/docs/messaging/compliance/a2p-10dlc/onboarding-isv-api
- **SHAKEN/STIR ISV**: https://www.twilio.com/docs/voice/trusted-calling-with-shakenstir/shakenstir-onboarding/shaken-stir-trust-hub-api-isvs-subaccounts
- **Secondary Customer Profile**: https://www.twilio.com/docs/trust-hub/trusthub-rest-api/api-create-secondary-customer-profile
- **Trust Product Evaluations**: https://www.twilio.com/docs/trust-hub/trusthub-rest-api/trust-products/evaluations-tp
