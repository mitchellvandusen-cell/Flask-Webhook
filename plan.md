# Salesforce CRM Provider — Implementation Plan

## Overview

Build a full Salesforce CRM provider following the exact same architecture as the HubSpot provider (`crm_providers/hubspot/`). This gives Salesforce users the same depth of integration: OAuth 2.0, real-time webhooks, data sync, activity logging, contact resolution, and embeddable CRM sidebar.

The existing `crm_adapters/salesforce_adapter.py` already handles outbound operations (SOQL search, Event booking, Task logging, Contact CRUD). The new provider wraps and extends this with inbound webhook handling, OAuth lifecycle, data sync, and CRM card support.

---

## File Structure

```
crm_providers/salesforce/
├── __init__.py      # SalesforceProvider orchestrator (mirrors hubspot/__init__.py)
├── oauth.py         # OAuth 2.0 Web Server flow (initiate, callback, token refresh)
├── inbound.py       # Salesforce webhook handler (Outbound Messages or Platform Events)
├── sync.py          # Data sync engine (Tasks, Events, Opportunities, Contacts → local Postgres)
├── logger.py        # Activity logging (SMS → Task, calls → Task, notes → Note)
├── resolver.py      # Contact resolution via SOQL search
└── crm_card.py      # Salesforce Canvas App / Lightning sidebar data
```

Plus:
- `blueprints/salesforce.py` — new blueprint for OAuth routes + webhook endpoint
- Updates to `crm_providers/__init__.py` — register "salesforce" in PROVIDER_REGISTRY
- Updates to `blueprints/dashboard.py` — Salesforce in CRM picker
- Updates to `main.py` — register salesforce blueprint

---

## Step 1: Provider Orchestrator (`crm_providers/salesforce/__init__.py`)

Create `SalesforceProvider(CRMProvider)` with:

```python
CRM_NAME = "Salesforce"
CRM_TYPE = "salesforce"

HAS_INBOUND_WEBHOOKS = True
HAS_OAUTH = True
HAS_DATA_SYNC = True
HAS_ACTIVITY_LOGGING = True
HAS_MARKETPLACE = False  # No AppExchange listing yet
HAS_EMBEDDABLE_UI = True  # Lightning sidebar card
```

Methods delegate to submodules exactly like HubSpot:
- `normalize_webhook()` → `inbound.normalize_salesforce_event()`
- `verify_webhook_signature()` → verify Salesforce webhook signature
- `get_valid_token()` → check `crm_config.token_expires_at`, refresh if needed
- `refresh_token()` → `oauth.refresh_salesforce_token()`
- `sync_conversations()` → `sync.sync_salesforce_tasks()` (Tasks = SMS log equivalent)
- `sync_deals()` → `sync.sync_salesforce_opportunities()`
- `sync_contacts()` → `sync.sync_salesforce_contacts()`
- `log_outbound_sms()` → `logger.log_outbound_sms()`
- `log_call()` → `logger.log_call()`
- `log_note()` → `logger.log_note()`
- `resolve_contact()` → `resolver.resolve_contact()`
- `get_crm_card_data()` → `crm_card.get_intelligence()`

---

## Step 2: OAuth 2.0 Flow (`crm_providers/salesforce/oauth.py`)

Salesforce uses the **OAuth 2.0 Web Server Flow** (Authorization Code Grant):

### Functions:
- `get_oauth_initiate_url(state)` — Build Salesforce authorization URL
  - Endpoint: `https://login.salesforce.com/services/oauth2/authorize`
  - Scopes: `api refresh_token`
  - Response type: `code`
  - Redirect URI: `https://{YOUR_DOMAIN}/salesforce/oauth/callback`

- `exchange_code_for_tokens(code)` — Exchange auth code for tokens
  - POST to `https://login.salesforce.com/services/oauth2/token`
  - Returns: `access_token`, `refresh_token`, `instance_url`, `id` (user identity URL)
  - Store in `subscribers.crm_config`: `{access_token, refresh_token, instance_url, token_expires_at, sf_user_id}`

- `refresh_salesforce_token(subscriber)` — Refresh expired token
  - POST to `https://login.salesforce.com/services/oauth2/token` with `grant_type=refresh_token`
  - Salesforce access tokens expire after **~2 hours** (session timeout configurable)
  - Use `update_crm_config_token()` to persist without clobbering other fields
  - Return `{access_token, token_expires_at}` or `None`

### Environment Variables:
- `SALESFORCE_CLIENT_ID` — Connected App consumer key
- `SALESFORCE_CLIENT_SECRET` — Connected App consumer secret
- `SALESFORCE_REDIRECT_URI` (derived from `YOUR_DOMAIN`)

### Token Storage:
```json
{
  "access_token": "00D...",
  "refresh_token": "5Aep...",
  "instance_url": "https://na1.salesforce.com",
  "token_expires_at": 1711234567,
  "sf_user_id": "005..."
}
```

---

## Step 3: Blueprint Routes (`blueprints/salesforce.py`)

New Flask blueprint `salesforce_bp`:

| Route | Method | Purpose |
|-------|--------|---------|
| `/salesforce/oauth/initiate` | GET | Start OAuth flow → redirect to Salesforce |
| `/salesforce/oauth/callback` | GET | Exchange code → store tokens in `crm_config` |
| `/salesforce/webhook` | POST | Receive Salesforce Outbound Messages / Platform Events |
| `/salesforce/crm-card` | GET | Lightning Web Component data endpoint (AI intelligence) |
| `/salesforce/crm-card/health` | GET | Health check for CRM card |

### OAuth Flow:
1. User clicks "Connect Salesforce" in dashboard
2. `GET /salesforce/oauth/initiate` → redirect to `login.salesforce.com/services/oauth2/authorize`
3. User authorizes → Salesforce redirects to `GET /salesforce/oauth/callback`
4. Callback exchanges code → stores tokens + `instance_url` in `subscribers.crm_config`
5. Sets `subscribers.crm_type = 'salesforce'`

### Webhook Endpoint:
Salesforce has two webhook mechanisms:
1. **Outbound Messages** (Workflow Rules) — SOAP XML payloads
2. **Platform Events** (Event-Driven) — REST streaming via CometD / Pub/Sub API

For v1, use **Outbound Messages** (simpler, no streaming):
- Parse SOAP XML envelope from Salesforce
- Extract event type, object ID, changed fields
- Queue to RQ `production` queue like GHL/HubSpot

### Registration in `main.py`:
```python
from blueprints.salesforce import salesforce_bp
app.register_blueprint(salesforce_bp)
```

---

## Step 4: Inbound Webhook Handler (`crm_providers/salesforce/inbound.py`)

### Functions:
- `normalize_salesforce_event(request_data, subscriber)` — Parse Salesforce webhook → canonical format
  - Map Salesforce objects to IGB events:
    - `Contact` created → `ContactCreate`
    - `Contact` updated → `ContactUpdate`
    - `Lead` created → `ContactCreate` (Salesforce has both Leads and Contacts)
    - `Opportunity` stage changed → `DealUpdate`
    - `Task` created (SMS-related) → `InboundMessage`

- `verify_salesforce_signature(request_body, headers)` — Verify Salesforce webhook authenticity
  - For Outbound Messages: verify Salesforce org ID from SOAP envelope
  - For Platform Events: verify JWT signature

### Event Type Map:
```python
SALESFORCE_EVENT_MAP = {
    "contact_created": "ContactCreate",
    "sms_received": "InboundMessage",
    "field_updated": "ContactUpdate",
    "stage_changed": "DealUpdate",
    "lead_created": "LeadCreate",
}
```

---

## Step 5: Data Sync Engine (`crm_providers/salesforce/sync.py`)

Mirrors `crm_providers/hubspot/sync.py` patterns:

### Functions:
- `sync_salesforce_contacts(location_id, token, since=None)` → `contacts` table
  - SOQL: `SELECT Id, FirstName, LastName, Email, Phone, MobilePhone, CreatedDate, LastModifiedDate FROM Contact WHERE LastModifiedDate > {since} ORDER BY LastModifiedDate LIMIT 200`
  - Paginate via `nextRecordsUrl`
  - UPSERT into `contacts` with `crm_source='salesforce'`

- `sync_salesforce_opportunities(location_id, token, since=None)` → `crm_deals` table
  - SOQL: `SELECT Id, Name, StageName, Amount, CloseDate, ContactId, CreatedDate, LastModifiedDate FROM Opportunity WHERE LastModifiedDate > {since}`
  - Map `StageName` to pipeline stage, `Amount` to monetary_value
  - UPSERT into `crm_deals` with `crm_source='salesforce'`

- `sync_salesforce_tasks(location_id, token, since=None)` → `crm_conversations` table
  - SOQL: `SELECT Id, Subject, Description, WhoId, Status, ActivityDate, CreatedDate FROM Task WHERE LastModifiedDate > {since} AND Subject LIKE '%SMS%'`
  - Map Task to conversation record (IGB logs SMS as Tasks)
  - UPSERT into `crm_conversations` with `crm_source='salesforce'`

### Shared Patterns (from HubSpot sync):
- `_api_get(instance_url, token, endpoint, params)` with exponential backoff
- 401 auto-refresh via `refresh_salesforce_token()`
- 429 rate limit handling (Salesforce: daily API limit, not per-second)
- Cursor tracking in `crm_sync_state` table
- Batch UPSERT with `INSERT ... ON CONFLICT DO UPDATE`

### Salesforce API Details:
- Base URL: `{instance_url}/services/data/v66.0/`
- SOQL queries via `GET /query/?q=...`
- Pagination: response includes `nextRecordsUrl` if more results exist
- Rate limit: Daily API request limit (varies by edition: 15k-100k+/day)

---

## Step 6: Activity Logger (`crm_providers/salesforce/logger.py`)

Log IGB activity back to Salesforce timeline:

### Functions:
- `log_outbound_sms(contact_id, message, token, instance_url, **kwargs)` → Create Task
  - POST to `/sobjects/Task`
  - `Subject`: "SMS Sent via InsuranceGrokBot"
  - `Description`: message body
  - `WhoId`: contact_id (polymorphic lookup)
  - `Status`: "Completed"
  - `Type`: "Other" (or custom picklist value)
  - `ActivityDate`: today

- `log_call(contact_id, direction, duration, token, instance_url, **kwargs)` → Create Task
  - POST to `/sobjects/Task`
  - `Subject`: "Call via InsuranceGrokBot ({direction})"
  - `Description`: includes duration, recording URL if available
  - `CallDurationInSeconds`: duration
  - `Type`: "Call"

- `log_note(contact_id, note, token, instance_url, **kwargs)` → Create ContentNote + link
  - POST to `/sobjects/ContentNote` with note body
  - POST to `/sobjects/ContentDocumentLink` to link note to Contact

### Salesforce Object Mapping:
| IGB Action | Salesforce Object | Key Fields |
|------------|------------------|------------|
| Outbound SMS | Task | WhoId, Subject, Description, Status="Completed" |
| Call | Task (Type=Call) | WhoId, CallDurationInSeconds, Subject |
| Note | ContentNote + ContentDocumentLink | Title, Content, LinkedEntityId |

---

## Step 7: Contact Resolver (`crm_providers/salesforce/resolver.py`)

### Functions:
- `resolve_contact(phone, name, email, access_token, instance_url)` → dict or None
  - Build SOQL with OR conditions (priority: email > phone > name)
  - Phone normalization: strip to digits, handle +1 prefix, search both `Phone` and `MobilePhone`
  - Return `{id, firstName, lastName, email, phone}`

Largely wraps the existing `SalesforceAdapter.search_contact()` but adds MobilePhone search and name lookup.

---

## Step 8: CRM Card / Lightning Sidebar (`crm_providers/salesforce/crm_card.py`)

### Functions:
- `get_intelligence(contact_id, location_id)` → dict
  - Read from `contact_intelligence` cache (zero AI cost)
  - Return: temperature, score, summary, recommended actions
  - Same logic as HubSpot CRM card

### Route:
- `GET /salesforce/crm-card?contactId={id}` — returns JSON for Lightning Web Component
- Lightning component fetches this URL and renders AI intelligence in sidebar

### Salesforce Setup (future):
- Lightning Web Component (LWC) that embeds in Contact record page
- Or Visualforce page with iframe to `/embed/intelligence/{contact_id}`
- This is simpler to ship first — just use the existing embed routes

---

## Step 9: Provider Registration

### `crm_providers/__init__.py`:
```python
PROVIDER_REGISTRY = {
    "ghl": ("crm_providers.ghl", "GHLProvider"),
    "gohighlevel": ("crm_providers.ghl", "GHLProvider"),
    "hubspot": ("crm_providers.hubspot", "HubSpotProvider"),
    "salesforce": ("crm_providers.salesforce", "SalesforceProvider"),  # NEW
}
```

### `db.py`:
- No schema changes needed — `crm_conversations`, `crm_deals`, `contacts` tables already have `crm_source` column
- Just use `crm_source='salesforce'` for all Salesforce data

### `blueprints/dashboard.py`:
- Add "Salesforce" to CRM type picker in Connect/Integrations tab
- Show OAuth connect button when `crm_type='salesforce'`

---

## Step 10: Environment Variables

Add to `.env.example`:
```bash
# Salesforce Connected App
SALESFORCE_CLIENT_ID=your_connected_app_consumer_key
SALESFORCE_CLIENT_SECRET=your_connected_app_consumer_secret
```

No `SALESFORCE_REDIRECT_URI` needed — derived from `YOUR_DOMAIN` at runtime.

---

## Step 11: Cron Integration

Update `blueprints/cron.py`:
- `refresh-tokens` job: add Salesforce token refresh (check `crm_type='salesforce'` subscribers)
- `sync-ghl-data` job: rename to `sync-crm-data`, include Salesforce subscribers

---

## Step 12: Salesforce Connected App Setup Guide

Document in CLAUDE.md (similar to HubSpot Developer App Setup Guide):
1. Create Connected App in Salesforce Setup
2. Configure OAuth scopes: `api`, `refresh_token`
3. Set callback URL to `https://{YOUR_DOMAIN}/salesforce/oauth/callback`
4. Copy Consumer Key + Secret to env vars
5. Enable Outbound Messages for Contact/Lead/Opportunity changes
6. Point Outbound Messages to `https://{YOUR_DOMAIN}/salesforce/webhook`

---

## Implementation Order

1. **`crm_providers/salesforce/__init__.py`** — Provider skeleton with capability flags
2. **`crm_providers/salesforce/oauth.py`** — OAuth flow (highest priority — users need to connect)
3. **`blueprints/salesforce.py`** — Routes for OAuth + webhook
4. **`crm_providers/salesforce/resolver.py`** — Contact search (needed by pipeline)
5. **`crm_providers/salesforce/logger.py`** — Activity logging (SMS/call/note back to SF)
6. **`crm_providers/salesforce/sync.py`** — Data sync (conversations, deals, contacts)
7. **`crm_providers/salesforce/inbound.py`** — Inbound webhook handling
8. **`crm_providers/salesforce/crm_card.py`** — CRM sidebar card
9. **Provider registration** — Update `__init__.py`, `main.py`, dashboard CRM picker
10. **Cron updates** — Token refresh + data sync for Salesforce subscribers
11. **Testing** — End-to-end with Salesforce Developer Edition (free)

---

## Key Differences from HubSpot

| Aspect | HubSpot | Salesforce |
|--------|---------|------------|
| Token expiry | 6 hours | ~2 hours (session-based) |
| Webhook format | JSON array (batched) | SOAP XML (Outbound Messages) or Platform Events |
| Webhook signature | HMAC-SHA256 v3 | Org ID verification or JWT |
| Rate limits | 40 req/10s (OAuth) | Daily limit (15k-100k+/day) |
| Conversations | Communication object | Task object (SMS logged as Tasks) |
| Deals | Deal object | Opportunity object |
| Notes | Note engagement | ContentNote + ContentDocumentLink |
| CRM Card | CRM Card v3 JSON | Lightning Web Component or Visualforce |
| Contact model | Contact only | Lead + Contact (separate objects, convertible) |
| Search API | CRM v3 Search API (POST) | SOQL (GET /query) |
| Pagination | `after` cursor param | `nextRecordsUrl` |

---

## Risks & Mitigations

1. **SOAP XML parsing** — Salesforce Outbound Messages use SOAP. Mitigation: use `xml.etree.ElementTree` for parsing, or consider Platform Events (REST/JSON) instead.
2. **Lead vs Contact duality** — Salesforce has both Leads and Contacts. Mitigation: search both objects, prefer Contact if converted.
3. **Daily API limits** — Salesforce has daily limits, not per-second. Mitigation: batch SOQL queries, use Composite API for multi-object operations.
4. **Connected App approval** — Salesforce orgs may require admin approval for Connected Apps. Document this clearly in setup guide.
