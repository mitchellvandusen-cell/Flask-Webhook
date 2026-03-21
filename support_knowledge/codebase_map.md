# InsuranceGrokBot Codebase Map

## Core Application

| File | Purpose | Common Failure Points |
|------|---------|----------------------|
| main.py | Flask app factory — wires blueprints, Redis, DB, Flask-Login | Blueprint registration errors, missing env vars |
| db.py / db/ | PostgreSQL layer — connection pool, all data access | Pool exhaustion, connection leaks (missing return_db_connection) |
| db/pool.py | ThreadedConnectionPool (min=2, max=20, 500 waiters, 10s timeout) | Pool timeout under load |
| db/schema.py | Runs Alembic migrations via init_db() | Migration conflicts, missing revision chain |
| db_legacy.py | Legacy DB compatibility shim | Connection not returned in finally block |
| tasks.py | Background job engine — AI pipeline, webhook processing | Token expiry, LLM failures, spaCy model loading |
| extensions.py | Shared Redis/RQ initialization, queue definitions | Redis connection failures |
| worker.py | RQ worker startup script | Queue name mismatch, import errors |

## AI & Intelligence

| File | Purpose | Common Failure Points |
|------|---------|----------------------|
| llm_caller.py | Clean LLM invocation wrapper (non-reasoning model) | XAI_API_KEY missing, model unavailable |
| prompt.py | System prompt builder for sales bot | Prompt too long for context window |
| conversation_engine.py | Stage analysis, objection classification | Regex false positives on short messages |
| sales_director.py | Strategic directive generator | Objection phase detection mismatch |
| lead_intelligence.py | AI lead scoring + temperature + caching | Cache invalidation, bulk LLM failures |
| memory.py | Contact memory/facts retrieval | spaCy model not loaded |
| reply_sanitizer.py | Sanitize LLM output before sending | Overly aggressive blocking |
| booking_detection.py | Calendar booking intent detection | False positive booking triggers |
| classification_memory.py | TF-IDF classification learning | Vectorizer dimension mismatch |

## Voice & Dialer

| File | Purpose | Common Failure Points |
|------|---------|----------------------|
| voice_server.py | Standalone async FastAPI voice WebSocket bridge | WebSocket drops, async state loss |
| voice_bridge.py | Backward-compat shim → re-exports from voice/ | Import errors if voice/ structure changes |
| voice/stream.py | Call audio WebSocket bridge | Audio codec issues, listener queue cleanup |
| voice/async_stream.py | Async version of stream for FastAPI | Same as stream.py but async |
| voice/outbound.py | Outbound call initiation, TwiML | Wrong TwiML app SID, number format |
| voice/twiml_routes.py | TwiML webhook handlers — status, gather, transfer | Call state race conditions |
| voice/dialer.py | Multi-line power dialer, predictive | Subscription tier gating, concurrent call limits |
| voice/predictive_engine.py | Erlang-C pacing, TCPA compliance, agent state | Division by zero on empty history |
| voice/numbers.py | Phone number management, Trust Hub, spam protection | Twilio API errors, ISV account confusion |
| voice/a2p.py | A2P 10DLC registration routes | Brand/campaign rejection |
| voice/contacts.py | Contact data fetching, engagement metrics | GHL API rate limits |
| voice/call_history.py | Call history CRUD, recordings | Missing recording SIDs |
| voice/redis_state.py | Redis-backed call state (active_calls, transfers) | Redis connection lost, JSON serialization |
| voice/call_state.py | Re-exports from redis_state, in-process listeners | Import circular dependencies |
| voice/voice_prompt.py | Voice AI system prompt builder | Prompt context overflow |
| voice/voice_tools.py | Voice AI function calling tools | Calendar API failures |
| voice/stats.py | Call statistics and KPIs | Timezone conversion errors |
| voice/recordings.py | Recording playback, transcription | Twilio recording URL auth |
| voice/audio.py | Audio pipeline — soxr resampling, mulaw/PCM | Missing soxr/scipy dependencies |
| voice/intelligence.py | AI contact intelligence routes | Bulk analysis timeout |
| voice/helpers.py | Shared voice utilities | Subscriber lookup failures |
| voice/insights.py | Call insights and analytics | Twilio Insights API access |
| voice/setup.py | Voice blueprint setup | Twilio client init failures |

## Blueprints (HTTP Routes)

| File | Purpose | Common Failure Points |
|------|---------|----------------------|
| blueprints/auth.py | Login, register, password reset, claim-account | itsdangerous token expiry, email delivery |
| blueprints/public.py | Marketing pages — home, comparison, FAQ | Template rendering errors |
| blueprints/webhooks.py | GHL webhook receiver, support bot endpoint | Signature verification, token expiry |
| blueprints/oauth.py | GHL OAuth initiate/callback, token refresh | State parameter mismatch, scope errors |
| blueprints/dashboard.py | Main dashboard, config save, bot settings | Session expiry, voice_config JSONB merge |
| blueprints/billing.py | Stripe webhooks, checkout, plan switching | Webhook signature, proration calc |
| blueprints/admin.py | God Mode admin dashboard, impersonation | ADMIN_EMAILS whitelist |
| blueprints/agency.py | Agency dashboard, sub-user invites, KPIs | Agency billing record missing |
| blueprints/demo.py | Demo chat endpoints | Session state loss |
| blueprints/discord.py | Discord OAuth, servers, channels, messages | Bot not in guild |
| blueprints/slack.py | Slack OAuth, workspace management | Token expiry |
| blueprints/cron.py | Scheduled jobs — token refresh, sync, recovery | CRON_SECRET auth, job timeout |
| blueprints/inbox.py | Unified inbox, SSE notifications | SSE connection drops |
| blueprints/calendar.py | GHL calendar fetching and booking | Calendar ID not found |
| blueprints/google_calendar.py | Google Calendar OAuth | Redirect URI mismatch |
| blueprints/team.py | Team management — invites, roles, permissions | location_users FK violations |
| blueprints/embed.py | Embeddable panel routes for CRM iframes | CORS, auth context |
| blueprints/workflows.py | Workflow CRUD, AI builder, templates | Workflow engine exceptions |
| blueprints/contacts_import.py | Contact import from CSV/CRM | CSV parsing, duplicate detection |
| blueprints/ghl_embed.py | GHL embeddable dialer/panel | Authentication in iframe |

## CRM Integration

| File | Purpose | Common Failure Points |
|------|---------|----------------------|
| crm_providers/base.py | CRMProvider ABC — interface | Not implementing required methods |
| crm_providers/ghl/__init__.py | GHL provider wrapper | Token refresh race conditions |
| crm_providers/hubspot/ | HubSpot full integration (OAuth, sync, webhooks, CRM card) | 6-hour token expiry, HMAC verification |
| crm_adapters/factory.py | Adapter factory | Unknown CRM type |
| ghl_api.py | GHL OAuth token management + API helpers | Token refresh loops |
| ghl_calendar.py | GHL calendar booking operations | Slot already taken |
| ghl_message.py | GHL SMS delivery | 401/403 on expired token |
| ghl_logger.py | Log IGB messages/calls to GHL conversation | Conversation provider ID mismatch |
| ghl_sync.py | GHL data sync engine | Rate limits, cursor tracking |

## Phone System & SMS

| File | Purpose | Common Failure Points |
|------|---------|----------------------|
| twilio_provisioning.py | Sub-account provisioning, Trust Hub, A2P, CNAM, Voice Integrity | ISV/direct account confusion, SDK status bug |
| twilio_sms.py | Direct Twilio SMS sender (bypass GHL) | Auth errors, rate limits, invalid numbers |
| number_health.py | Phone number health tracking, smart rotation | Missing health records |

## Support System

| File | Purpose | Common Failure Points |
|------|---------|----------------------|
| support_bot.py | Support bot logic: diagnostics, tickets, sanitizer | DB connection errors |
| support_prompt.py | System prompt for support bot | Prompt too large |
| support_tools.py | Grok function-calling tools for support agent | Tool execution failures, Redis unavailable |

## Other

| File | Purpose | Common Failure Points |
|------|---------|----------------------|
| api_v1.py | External API (OpenAI-compatible + Training API) | Rate limiting, token auth |
| email_templates.py | HTML email builder | Template rendering |
| send_email_api.py | Mailgun API email sender | API key, domain config |
| webhook_delivery.py | Outbound webhook delivery with HMAC | Timeout, retry exhaustion |
| workflow_engine.py | Workflow execution engine (~2300 lines) | Step handler errors, cron triggers |
| token_encryption.py | Fernet encryption for OAuth tokens | Key rotation |
| carrier_list.py | 63 insurance carriers for UI picker | — |
| insurance_companies.py | 270+ carrier names for AI detection | — |
| insurance_knowledge.py | Deep product knowledge for AI context | — |
| underwriting.py | Live carrier underwriting rules | Google Sheets API access |
| translations.py | Multi-language UI strings | Missing translation keys |
| age.py | DOB-to-age calculator | Date parsing |
| contact_validator.py | Contact ID validation/resolution | Invalid contact format |
| individual_profile.py | Comprehensive contact profile builder | Data aggregation errors |
| lead_resolver.py | Smart lead type detection | Tag parsing |
| message_utils.py | Message batching utilities | — |
| payload_utils.py | Webhook payload normalization | Missing fields in GHL payload |
| forms.py | Flask-WTF form definitions | CSRF validation |
| utils.py | JSON serialization helpers | — |
| sync_subscribers.py | Syncs subscriber DB from Google Sheets | Sheets API access |
| error_feed.py | Redis-based error feed for monitoring | Redis connection |

## Database Tables (30+)

Key tables for support diagnostics:
- subscribers — Master user table (location_id, email, tokens, config, tier)
- webhook_logs — Activity/audit log per location (event_type, status, details)
- contact_messages — Chat history per contact
- call_history — Voice call records
- support_tickets — AI-created support tickets
- support_actions_log — Audit trail for support bot write actions
- support_log_cache — Cached server logs for support diagnostics
- number_health — Phone number health tracking
- contact_intelligence — AI intelligence cache (temperature, score, summary)
- agency_billing — Agency owner records
- location_users — Team members per location
